from __future__ import annotations

import json
from pathlib import Path

from logscraper.config import AppSettings
from logscraper.dotnet.repo_analyzer import DotNetRepoAnalyzer
from logscraper.llm.client import LLMClient
from logscraper.models import CodeFixPlan, ResolutionTask


class DotNetAnalyzerAgent:
    def __init__(self, settings: AppSettings, llm_client: LLMClient | None = None) -> None:
        self.settings = settings
        self.llm_client = llm_client or LLMClient(settings)

    def analyze(self, task: ResolutionTask, repo_path: str | Path | None = None) -> CodeFixPlan:
        event = task.log_event
        first_frame = event.frames[0] if event.frames else None
        root_hint = "No stack frame was available."
        if first_frame:
            member = ".".join(
                part
                for part in [first_frame.namespace, first_frame.class_name, first_frame.method_name]
                if part
            )
            root_hint = f"The top .NET stack frame points at {member or first_frame.raw}."

        primary_message = event.exception_message or event.error_message
        root_cause_summary = f"{event.exception_type or 'Unknown exception'}: {primary_message}. {root_hint}"
        error_context = self._error_context(task, root_hint)
        repo = self._repo_path(repo_path)
        if not repo:
            llm_fix = self.llm_client.suggest_fix(error_context=error_context, source_context="")
            confidence = self._compute_confidence(
                frames=event.frames,
                candidates_found=0,
                has_repo=False,
                llm_responded=llm_fix is not None,
                exception_type=event.exception_type,
                occurrence_count=task.occurrence_count,
            )
            return CodeFixPlan(
                fingerprint=task.fingerprint,
                root_cause_summary=root_cause_summary,
                files_to_inspect=[],
                files_allowed_to_edit=[],
                suggested_fix=llm_fix
                or "Configure GitHub repository settings or DOTNET_REPO_PATH so the agent can map stack frames to C# files.",
                suggested_tests=[],
                confidence=confidence,
            )

        print(f"[analyzer] scanning repo path={repo}")
        analyzer = DotNetRepoAnalyzer(repo)
        candidates = analyzer.find_candidate_files(event.frames)
        suggested_tests = analyzer.suggest_test_commands(candidates)
        candidate_strings = [self._relative(repo, path) for path in candidates]
        print(f"[analyzer] mapped candidate_files={len(candidate_strings)}")

        suggested_fix = (
            "Inspect the candidate C# file(s), reproduce the failing path, and make the smallest change "
            "connected to the parsed stack frame."
        )
        if event.exception_type and "NullReferenceException" in event.exception_type:
            suggested_fix = (
                "Check the top stack-frame method for nullable inputs or missing dependency state, "
                "then add a narrow guard or fix the upstream initialization path."
            )

        source_context = self._source_context(repo, candidates)
        llm_fix = self.llm_client.suggest_fix(error_context=error_context, source_context=source_context)
        if llm_fix:
            suggested_fix = llm_fix

        confidence = self._compute_confidence(
            frames=event.frames,
            candidates_found=len(candidate_strings),
            has_repo=True,
            llm_responded=llm_fix is not None,
            exception_type=event.exception_type,
            occurrence_count=task.occurrence_count,
        )

        return CodeFixPlan(
            fingerprint=task.fingerprint,
            root_cause_summary=root_cause_summary,
            files_to_inspect=candidate_strings,
            files_allowed_to_edit=candidate_strings,
            suggested_fix=suggested_fix,
            suggested_tests=suggested_tests,
            confidence=confidence,
        )

    # Well-understood .NET exceptions that are typically straightforward to fix.
    _KNOWN_FIXABLE_EXCEPTIONS = {
        "NullReferenceException",
        "ArgumentNullException",
        "ArgumentException",
        "ArgumentOutOfRangeException",
        "InvalidOperationException",
        "IndexOutOfRangeException",
        "KeyNotFoundException",
        "DivideByZeroException",
        "ObjectDisposedException",
        "NotImplementedException",
    }

    def _compute_confidence(
        self,
        *,
        frames: list,
        candidates_found: int,
        has_repo: bool,
        llm_responded: bool,
        exception_type: str | None,
        occurrence_count: int,
    ) -> float:
        """Signal-based confidence scoring.

        Combines multiple signals into a 0.0–0.95 score:
        - base:             repo available (0.30) or not (0.10)
        - frame_quality:    parsed frames with class/method info (up to 0.20)
        - file_match:       source files mapped from stack trace (up to 0.25)
        - llm_response:     LLM provided a fix suggestion (0.10)
        - known_exception:  well-understood exception type (0.10)
        - occurrence:       seen multiple times = reproducible (up to 0.05)
        """
        # Base: is a repo available for analysis?
        score = 0.30 if has_repo else 0.10

        # Frame quality: how many frames have structured namespace/class/method?
        rich_frames = sum(
            1 for f in frames
            if (f.namespace or f.class_name) and f.method_name
        )
        if rich_frames >= 3:
            score += 0.20
        elif rich_frames >= 1:
            score += 0.10 + min(rich_frames, 3) * 0.033

        # File match: did we map stack frames to actual source files?
        if candidates_found >= 2:
            score += 0.25
        elif candidates_found == 1:
            score += 0.15

        # LLM response: did the LLM provide analysis?
        if llm_responded:
            score += 0.10

        # Known exception type: well-understood and typically fixable
        if exception_type:
            short_name = exception_type.rsplit(".", 1)[-1]
            if short_name in self._KNOWN_FIXABLE_EXCEPTIONS:
                score += 0.10

        # Occurrence count: multiple sightings = reproducible
        if occurrence_count >= 10:
            score += 0.05
        elif occurrence_count >= 3:
            score += 0.03

        final = min(score, 0.95)
        print(f"[analyzer] confidence={final:.2f} (frames={len(frames)} rich={rich_frames} "
              f"candidates={candidates_found} repo={has_repo} llm={llm_responded} "
              f"exception={exception_type} occurrences={occurrence_count})")
        return round(final, 2)

    def _repo_path(self, repo_path: str | Path | None) -> Path | None:
        if repo_path:
            return Path(repo_path).expanduser().resolve()
        return self.settings.dotnet_repo

    def _error_context(self, task: ResolutionTask, root_hint: str) -> str:
        event = task.log_event
        parts = [
            f"Fingerprint: {task.fingerprint}",
            f"Service: {event.service or 'unknown'}",
            f"Environment: {event.environment or 'unknown'}",
            f"Exception: {event.exception_type or 'unknown'}",
            f"Log message: {event.error_message or 'n/a'}",
            f"Exception message: {event.exception_message or 'n/a'}",
            "Detailed exception:",
            self._limit_text(event.detailed_exception or "n/a", limit=6000),
            "Parsed stack frames:",
            self._stack_frame_text(event.frames),
            "Structured log fields:",
            self._limit_text(
                self._json_text(event.log_fields or event.log_attributes or event.raw_payload),
                limit=6000,
            ),
            root_hint,
            "Stack trace:",
            self._limit_text(event.stack_trace or "n/a", limit=6000),
        ]
        return "\n".join(parts)

    def _source_context(self, repo: Path, candidates: list[Path]) -> str:
        snippets: list[str] = []
        for path in candidates[:3]:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            relative = self._relative(repo, path)
            snippets.append(f"File: {relative}\n{self._limit_text(text, limit=5000)}")
        return "\n\n".join(snippets)

    def _limit_text(self, value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _json_text(self, value: object) -> str:
        return json.dumps(value, default=str, sort_keys=True) if value else "{}"

    def _stack_frame_text(self, frames: object) -> str:
        frame_lines = [getattr(frame, "raw", "") for frame in frames or []]
        return "\n".join(line for line in frame_lines if line) or "n/a"

    def _relative(self, root: Path, path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)
