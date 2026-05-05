from __future__ import annotations

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

        root_cause_summary = f"{event.exception_type or 'Unknown exception'}: {event.error_message}. {root_hint}"
        error_context = self._error_context(task, root_hint)
        repo = self._repo_path(repo_path)
        if not repo:
            llm_fix = self.llm_client.suggest_fix(error_context=error_context, source_context="")
            return CodeFixPlan(
                fingerprint=task.fingerprint,
                root_cause_summary=root_cause_summary,
                files_to_inspect=[],
                files_allowed_to_edit=[],
                suggested_fix=llm_fix
                or "Configure GitHub repository settings or DOTNET_REPO_PATH so the agent can map stack frames to C# files.",
                suggested_tests=[],
                confidence=0.25 if llm_fix else 0.2,
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

        return CodeFixPlan(
            fingerprint=task.fingerprint,
            root_cause_summary=root_cause_summary,
            files_to_inspect=candidate_strings,
            files_allowed_to_edit=candidate_strings,
            suggested_fix=suggested_fix,
            suggested_tests=suggested_tests,
            confidence=0.75 if candidate_strings else 0.35,
        )

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
            f"Message: {event.error_message or 'n/a'}",
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

    def _relative(self, root: Path, path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)
