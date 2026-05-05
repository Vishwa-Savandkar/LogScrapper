from __future__ import annotations

from logscraper.models import CodeFixPlan, PullRequestResult, ReviewResult


class PRReviewerAgent:
    def review(self, plan: CodeFixPlan, result: PullRequestResult) -> ReviewResult:
        findings: list[str] = []
        if not plan.files_allowed_to_edit:
            findings.append("No C# file was confidently mapped from the stack trace.")
        if plan.confidence < 0.5:
            findings.append("Root-cause confidence is low; keep the result in dry-run review.")
        if not result.tests_run:
            findings.append("No local .NET build or test command was identified.")

        approved = not findings and plan.confidence >= 0.5
        residual_risk = (
            "Review mapped files, nullable behavior, async flow, dependency injection assumptions, and test coverage before merge."
        )
        return ReviewResult(approved=approved, findings=findings, residual_risk=residual_risk)
