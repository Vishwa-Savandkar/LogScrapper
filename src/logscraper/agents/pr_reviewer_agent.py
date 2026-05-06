from __future__ import annotations

from logscraper.llm.client import LLMClient
from logscraper.models import CodeFixPlan, PullRequestResult, ReviewResult


class PRReviewerAgent:
    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client

    def review(self, plan: CodeFixPlan, result: PullRequestResult) -> ReviewResult:
        findings: list[str] = []
        if not plan.files_allowed_to_edit:
            findings.append("No C# file was confidently mapped from the stack trace.")
        if plan.confidence < 0.5:
            findings.append("Root-cause confidence is low; keep the result in dry-run review.")
        if not result.tests_run:
            findings.append("No local .NET build or test command was identified.")

        # LLM-powered semantic review
        llm_findings = self._llm_review(plan, result)
        if llm_findings:
            findings.extend(llm_findings)

        approved = not findings and plan.confidence >= 0.5
        residual_risk = (
            "Review mapped files, nullable behavior, async flow, dependency injection assumptions, and test coverage before merge."
        )
        return ReviewResult(approved=approved, findings=findings, residual_risk=residual_risk)

    def _llm_review(self, plan: CodeFixPlan, result: PullRequestResult) -> list[str]:
        if not self.llm_client or not self.llm_client.is_configured():
            return []
        if not result.files_changed:
            return []

        review_prompt = (
            f"Root cause: {plan.root_cause_summary}\n"
            f"Suggested fix: {plan.suggested_fix}\n"
            f"Files changed: {', '.join(result.files_changed)}\n"
            f"PR body:\n{result.pr_body}\n\n"
            "Review this automated .NET fix for: correctness, null safety, async issues, "
            "missing error handling, and test coverage. Return only a JSON list of finding strings. "
            'Example: ["Missing null check on line X", "Async method not awaited"]. '
            "Return [] if the fix looks correct."
        )
        response = self.llm_client.suggest_fix(error_context=review_prompt, source_context="")
        if not response:
            return []

        import json
        try:
            parsed = json.loads(response)
            if isinstance(parsed, list):
                return [str(f) for f in parsed if f]
        except (json.JSONDecodeError, TypeError):
            pass
        # If LLM didn't return valid JSON, treat entire response as a single finding
        return [response.strip()] if response.strip() else []
