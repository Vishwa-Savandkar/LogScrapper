from __future__ import annotations

from pathlib import Path

from logscraper.config import AppSettings
from logscraper.dotnet.repo_analyzer import DotNetRepoAnalyzer
from logscraper.models import CodeFixPlan, ResolutionTask


class DotNetAnalyzerAgent:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def analyze(self, task: ResolutionTask) -> CodeFixPlan:
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

        repo = self.settings.dotnet_repo
        if not repo:
            return CodeFixPlan(
                fingerprint=task.fingerprint,
                root_cause_summary=f"{event.exception_type or 'Unknown exception'}: {event.error_message}. {root_hint}",
                files_to_inspect=[],
                files_allowed_to_edit=[],
                suggested_fix="Configure DOTNET_REPO_PATH so the agent can map stack frames to C# files before proposing edits.",
                suggested_tests=[],
                confidence=0.2,
            )

        analyzer = DotNetRepoAnalyzer(repo)
        candidates = analyzer.find_candidate_files(event.frames)
        suggested_tests = analyzer.suggest_test_commands(candidates)
        candidate_strings = [self._relative(repo, path) for path in candidates]

        suggested_fix = (
            "Inspect the candidate C# file(s), reproduce the failing path, and make the smallest change "
            "connected to the parsed stack frame."
        )
        if event.exception_type and "NullReferenceException" in event.exception_type:
            suggested_fix = (
                "Check the top stack-frame method for nullable inputs or missing dependency state, "
                "then add a narrow guard or fix the upstream initialization path."
            )

        return CodeFixPlan(
            fingerprint=task.fingerprint,
            root_cause_summary=f"{event.exception_type or 'Unknown exception'}: {event.error_message}. {root_hint}",
            files_to_inspect=candidate_strings,
            files_allowed_to_edit=candidate_strings,
            suggested_fix=suggested_fix,
            suggested_tests=suggested_tests,
            confidence=0.75 if candidate_strings else 0.35,
        )

    def _relative(self, root: Path, path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)
