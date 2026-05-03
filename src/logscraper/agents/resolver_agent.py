from __future__ import annotations

import re

from logscraper.config import AppSettings
from logscraper.connectors.github_connector import GitHubConnector
from logscraper.models import CodeFixPlan, PullRequestResult, ResolutionTask


class ResolverAgent:
    def __init__(self, settings: AppSettings, github: GitHubConnector | None = None) -> None:
        self.settings = settings
        self.github = github or GitHubConnector(settings)

    def prepare_pull_request(self, task: ResolutionTask, plan: CodeFixPlan) -> PullRequestResult:
        event = task.log_event
        branch_name = self._branch_name(event.exception_type, task.fingerprint)
        commit_message = self._commit_message(event.exception_type, event.error_message)
        pr_title = self._pr_title(event.exception_type, event.error_message)
        pr_body = self._pr_body(task, plan)

        pr_url = None
        if not self.settings.dry_run:
            pr_url = self.github.create_pull_request(
                head_branch=branch_name,
                title=pr_title,
                body=pr_body,
            )

        return PullRequestResult(
            dry_run=self.settings.dry_run,
            branch_name=branch_name,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
            pr_url=pr_url,
            files_changed=[],
            tests_run=plan.suggested_tests,
        )

    def _branch_name(self, exception_type: str | None, fingerprint: str) -> str:
        label = (exception_type or "dotnet-error").split(".")[-1]
        label = re.sub(r"[^a-zA-Z0-9-]+", "-", label).strip("-").lower() or "dotnet-error"
        return f"logscraper/{label}-{fingerprint[:12]}"

    def _commit_message(self, exception_type: str | None, message: str) -> str:
        label = (exception_type or "dotnet error").split(".")[-1]
        clean_message = re.sub(r"\s+", " ", message).strip()
        if len(clean_message) > 72:
            clean_message = clean_message[:69].rstrip() + "..."
        return f"Fix {label}: {clean_message}" if clean_message else f"Fix {label}"

    def _pr_title(self, exception_type: str | None, message: str) -> str:
        label = (exception_type or "DotNet error").split(".")[-1]
        clean_message = re.sub(r"\s+", " ", message).strip()
        if len(clean_message) > 90:
            clean_message = clean_message[:87].rstrip() + "..."
        return f"Resolve {label}: {clean_message}" if clean_message else f"Resolve {label}"

    def _pr_body(self, task: ResolutionTask, plan: CodeFixPlan) -> str:
        event = task.log_event
        files = "\n".join(f"- `{path}`" for path in plan.files_allowed_to_edit) or "- None mapped yet"
        tests = "\n".join(f"- `{command}`" for command in plan.suggested_tests) or "- Not available"
        dry_run_note = (
            "Dry-run mode is enabled, so no GitHub writes or code edits were performed."
            if self.settings.dry_run
            else "Live mode was enabled for PR creation."
        )
        return "\n".join(
            [
                "## Error Context",
                f"- Fingerprint: `{task.fingerprint}`",
                f"- Service: `{event.service or 'unknown'}`",
                f"- Environment: `{event.environment or 'unknown'}`",
                f"- Exception: `{event.exception_type or 'unknown'}`",
                f"- Message: {event.error_message or 'n/a'}",
                "",
                "## Root Cause Analysis",
                plan.root_cause_summary,
                "",
                "## Proposed Fix",
                plan.suggested_fix,
                "",
                "## Files Allowed To Edit",
                files,
                "",
                "## Suggested Verification",
                tests,
                "",
                "## Safety",
                dry_run_note,
                "Residual risk: automated analysis may miss application-specific behavior; human review is required.",
            ]
        )
