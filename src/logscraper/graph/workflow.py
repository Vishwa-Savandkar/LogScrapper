from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, TypedDict

from logscraper.agents.dotnet_analyzer_agent import DotNetAnalyzerAgent
from logscraper.agents.log_scraper_agent import LogScraperAgent
from logscraper.agents.master_agent import MasterAgent
from logscraper.agents.pr_reviewer_agent import PRReviewerAgent
from logscraper.agents.resolver_agent import ResolverAgent
from logscraper.config import AppSettings
from logscraper.connectors.github_workspace import GitHubRepoWorkspace
from logscraper.models import (
    CodeFixPlan,
    ErrorStatus,
    LogEvent,
    ProcessedError,
    PullRequestResult,
    ResolutionTask,
    ReviewResult,
)
from logscraper.store.history_db import HistoryDB


class AgentState(TypedDict, total=False):
    log_events: list[LogEvent]
    current_event: LogEvent | None
    resolution_task: ResolutionTask | None
    code_fix_plan: CodeFixPlan | None
    pull_request_result: PullRequestResult | None
    review_result: ReviewResult | None
    repo_path: str | None
    errors: list[str]
    route: str | None
    processed_errors: list[ProcessedError]
    validation_passed: bool | None


class LogScraperWorkflow:
    def __init__(
        self,
        *,
        settings: AppSettings,
        log_scraper: LogScraperAgent,
        master: MasterAgent,
        analyzer: DotNetAnalyzerAgent,
        resolver: ResolverAgent,
        reviewer: PRReviewerAgent,
        repo_workspace: GitHubRepoWorkspace,
        history: HistoryDB,
    ) -> None:
        self.settings = settings
        self.log_scraper = log_scraper
        self.master = master
        self.analyzer = analyzer
        self.resolver = resolver
        self.reviewer = reviewer
        self.repo_workspace = repo_workspace
        self.history = history
        self._logger = logging.getLogger("logscraper.workflow")
        self._run_id = uuid.uuid4().hex[:12]

    def run(self, initial_state: AgentState | None = None) -> AgentState:
        state: AgentState = {
            "log_events": [],
            "current_event": None,
            "resolution_task": None,
            "code_fix_plan": None,
            "pull_request_result": None,
            "review_result": None,
            "repo_path": None,
            "errors": [],
            "route": None,
            "processed_errors": [],
            "validation_passed": None,
        }
        if initial_state:
            state.update(initial_state)

        graph = self.to_langgraph()
        if graph is not None:
            config = {"configurable": {"thread_id": f"logscraper-{self._run_id}"}}
            try:
                return graph.invoke(state, config=config)
            except Exception as exc:
                state.setdefault("errors", []).append(str(exc))
                # Retry once from checkpoint
                try:
                    return graph.invoke(None, config=config)
                except Exception:
                    return state

        raise RuntimeError(
            "LangGraph is required but not installed. Install it with: pip install langgraph"
        )

    def to_langgraph(self):
        try:
            from langgraph.graph import END, START, StateGraph
        except ModuleNotFoundError as exc:
            if exc.name == "langgraph" or str(exc.name).startswith("langgraph."):
                return None
            raise

        graph = StateGraph(AgentState)
        graph.add_node("check_pr_outcomes", self.check_pr_outcomes)
        graph.add_node("fetch_datadog_logs", self._fetch_node)
        graph.add_node("deduplicate_errors", self._deduplicate_node)
        graph.add_node("route_error", self.route_error)
        graph.add_node("prepare_repo_workspace", self.prepare_repo_workspace)
        graph.add_node("analyze_dotnet_code", self.analyze_dotnet_code)
        graph.add_node("apply_or_dry_run_fix", self.apply_or_dry_run_fix)
        graph.add_node("review_fix", self.review_fix)
        graph.add_node("update_history", self.update_history)

        graph.add_edge(START, "check_pr_outcomes")
        graph.add_edge("check_pr_outcomes", "fetch_datadog_logs")
        graph.add_edge("fetch_datadog_logs", "deduplicate_errors")
        graph.add_edge("deduplicate_errors", "route_error")
        graph.add_conditional_edges(
            "route_error",
            self._route_after_routing,
            {
                "actionable": "prepare_repo_workspace",
                "done": END,
            },
        )
        graph.add_edge("prepare_repo_workspace", "analyze_dotnet_code")
        graph.add_conditional_edges(
            "analyze_dotnet_code",
            self._route_after_confidence,
            {
                "confident": "apply_or_dry_run_fix",
                "low_confidence": "update_history",
            },
        )
        graph.add_edge("apply_or_dry_run_fix", "review_fix")
        graph.add_edge("review_fix", "update_history")
        graph.add_edge("update_history", "route_error")

        try:
            from langgraph.checkpoint.memory import MemorySaver
            return graph.compile(checkpointer=MemorySaver())
        except ImportError:
            return graph.compile()

    def get_route_graph(self):
        graph = self.to_langgraph()
        if graph is None:
            raise RuntimeError(
                "LangGraph is not installed; install project dependencies before rendering the workflow graph."
            )
        return graph

    def draw_mermaid(self) -> str:
        return self.get_route_graph().get_graph().draw_mermaid()

    def draw_mermaid_png(self, output_path: str | Path | None = None, **kwargs: Any) -> bytes:
        path = Path(output_path) if output_path else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        return self.get_route_graph().get_graph().draw_mermaid_png(
            output_file_path=str(path) if path is not None else None,
            **kwargs,
        )

    def write_graph(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in {".mmd", ".mermaid", ".md", ".txt"}:
            path.write_text(self.draw_mermaid(), encoding="utf-8")
            return path
        self.draw_mermaid_png(path)
        return path

    def _log(self, msg: str, **extra: Any) -> None:
        self._logger.info(msg, extra={"run_id": self._run_id, **extra})

    def check_pr_outcomes(self, state: AgentState) -> AgentState:
        """Reconcile open PRs — mark merged as resolved, closed as re-openable."""
        self._log("check_pr_outcomes started")
        from logscraper.connectors.github_connector import GitHubConnector
        github = GitHubConnector(self.settings)
        in_review = self.history.list_by_status(ErrorStatus.IN_REVIEW)
        for record in in_review:
            if not record.pr_url:
                continue
            pr_state = github.get_pr_state(record.pr_url)
            if pr_state == "merged":
                self.history.update_status(record.fingerprint, ErrorStatus.RESOLVED)
                self._log("pr_outcome", fingerprint=record.fingerprint, pr_state="merged", new_status="resolved")
            elif pr_state == "closed":
                self.history.update_status(record.fingerprint, ErrorStatus.OPEN)
                self._log("pr_outcome", fingerprint=record.fingerprint, pr_state="closed", new_status="open")
        self._log("check_pr_outcomes completed", checked=len(in_review))
        return state

    def _fetch_node(self, state: AgentState) -> AgentState:
        self._log("fetch_datadog_logs started")
        if state.get("log_events"):
            self._log("fetch_datadog_logs skipped", existing_events=len(state["log_events"]))
            return state
        log_events = self.fetch_datadog_logs()
        self._log("fetch_datadog_logs completed", events=len(log_events))
        self._log_fetched_events(log_events)
        return {**state, "log_events": log_events}

    def _deduplicate_node(self, state: AgentState) -> AgentState:
        self._log("deduplicate_errors started", events=len(state.get("log_events", [])))
        seen: set[str] = set()
        unique: list[LogEvent] = []
        for event in state.get("log_events", []):
            if event.fingerprint in seen:
                continue
            seen.add(event.fingerprint)
            unique.append(event)
        self._log("deduplicate_errors completed", unique_events=len(unique))
        return {**state, "log_events": unique}

    def _route_after_routing(self, state: AgentState) -> str:
        return "actionable" if state.get("resolution_task") else "done"

    def _route_after_confidence(self, state: AgentState) -> str:
        plan = state.get("code_fix_plan")
        threshold = self.settings.confidence_threshold
        if plan and plan.confidence >= threshold:
            self._log("confidence gate passed", confidence=plan.confidence, threshold=threshold)
            return "confident"
        self._log("confidence gate failed; skipping fix", confidence=plan.confidence if plan else 0.0,
                  threshold=threshold)
        return "low_confidence"

    def fetch_datadog_logs(self) -> list[LogEvent]:
        return self.log_scraper.fetch_events()

    def _log_fetched_events(self, events: list[LogEvent]) -> None:
        for index, event in enumerate(events, start=1):
            self._log(
                "fetched_event",
                index=index,
                timestamp=event.timestamp.isoformat(),
                status=event.log_level,
                inferred=event.inferred_severity,
                service=event.service,
                document_id=event.document_id,
                fingerprint=event.fingerprint,
                event_message=self._truncate(event.exception_message or event.error_message, limit=300),
                frames=len(event.frames),
            )

    def _truncate(self, value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _json_text(self, value: Any) -> str:
        return json.dumps(value, default=str, sort_keys=True)

    def route_error(self, state: AgentState) -> AgentState:
        self._log("route_error started", events=len(state.get("log_events", [])))
        state["current_event"] = None
        state["resolution_task"] = None
        for event in state.get("log_events", []):
            route, task = self.master.route_event(event)
            state["route"] = route
            self._log("route_error checked", route=route, fingerprint=event.fingerprint)
            if task:
                state["current_event"] = event
                state["resolution_task"] = task
                self.history.update_status(task.fingerprint, ErrorStatus.IN_ANALYSIS)
                self._log("route_error completed", selected_fingerprint=task.fingerprint)
                return state
        self._log("route_error completed; no actionable task")
        return state

    def prepare_repo_workspace(self, state: AgentState) -> AgentState:
        self._log("prepare_repo_workspace started")
        task = state.get("resolution_task")
        if task is None:
            self._log("prepare_repo_workspace skipped; no resolution task")
            return state

        if self.settings.github_token and self.settings.github_repository:
            repo_path = self.repo_workspace.prepare(task.fingerprint)
            state["repo_path"] = str(repo_path)
            self._log("prepare_repo_workspace completed", repo_path=str(repo_path))
            return state

        local_repo = self.settings.dotnet_repo
        if local_repo:
            state["repo_path"] = str(local_repo)
            self._log("prepare_repo_workspace completed", local_repo_path=str(local_repo))
            return state

        self._log("prepare_repo_workspace completed; no repo configured")
        return state

    def analyze_dotnet_code(self, state: AgentState) -> AgentState:
        self._log("analyze_dotnet_code started")
        task = state.get("resolution_task")
        if task is None:
            self._log("analyze_dotnet_code skipped; no resolution task")
            return state
        state["code_fix_plan"] = self.analyzer.analyze(task, repo_path=state.get("repo_path"))
        self.history.update_status(task.fingerprint, ErrorStatus.FIX_PLANNED)
        self._log("analyze_dotnet_code completed", fingerprint=task.fingerprint,
                  confidence=state["code_fix_plan"].confidence)
        return state

    def apply_or_dry_run_fix(self, state: AgentState) -> AgentState:
        self._log("apply_or_dry_run_fix started")
        task = state.get("resolution_task")
        plan = state.get("code_fix_plan")
        if task is None or plan is None:
            self._log("apply_or_dry_run_fix skipped; missing task or plan")
            return state
        state["pull_request_result"] = self.resolver.apply_or_dry_run_fix(
            task,
            plan,
            repo_path=state.get("repo_path"),
        )
        result = state["pull_request_result"]
        state["validation_passed"] = result.validation_passed
        self._log("apply_or_dry_run_fix completed",
                  dry_run=result.dry_run, branch=result.branch_name,
                  pr_url=result.pr_url, validation_passed=result.validation_passed)
        return state

    def review_fix(self, state: AgentState) -> AgentState:
        self._log("review_fix started")
        plan = state.get("code_fix_plan")
        result = state.get("pull_request_result")
        if plan is None or result is None:
            self._log("review_fix skipped; missing plan or pull request result")
            return state
        state["review_result"] = self.reviewer.review(plan, result)
        review = state["review_result"]
        self._log("review_fix completed", approved=review.approved, findings=len(review.findings))
        return state

    def update_history(self, state: AgentState) -> AgentState:
        self._log("update_history started")
        task = state.get("resolution_task")
        result = state.get("pull_request_result")
        plan = state.get("code_fix_plan")
        review = state.get("review_result")
        if task is None or result is None:
            self._log("update_history skipped; missing task or pull request result")
            return state
        if result.pr_url:
            self.history.attach_pr(task.fingerprint, result.pr_url, status=ErrorStatus.IN_REVIEW)
            self._log("update_history completed", pr_url=result.pr_url)
        else:
            self.history.update_status(task.fingerprint, ErrorStatus.FIX_PLANNED)
            self._log("update_history completed", status=ErrorStatus.FIX_PLANNED.value)

        # Accumulate processed error summary
        event = state.get("current_event")
        entry = ProcessedError(
            fingerprint=task.fingerprint,
            exception_type=event.exception_type if event else None,
            route=state.get("route") or "",
            confidence=plan.confidence if plan else 0.0,
            pr_url=result.pr_url,
            review_approved=review.approved if review else False,
            review_findings=review.findings if review else [],
            validation_passed=state.get("validation_passed"),
            outcome="pr_created" if result.pr_url else ("dry_run" if result.dry_run else "skipped"),
        )
        processed = list(state.get("processed_errors") or [])
        processed.append(entry)
        state["processed_errors"] = processed
        return state


def build_workflow(
    *,
    settings: AppSettings | None = None,
    log_scraper: LogScraperAgent | None = None,
    history: HistoryDB | None = None,
    analyzer: DotNetAnalyzerAgent | None = None,
    resolver: ResolverAgent | None = None,
    reviewer: PRReviewerAgent | None = None,
    repo_workspace: GitHubRepoWorkspace | None = None,
) -> LogScraperWorkflow:
    resolved_settings = settings or AppSettings()
    resolved_history = history or HistoryDB(resolved_settings.db_path)
    from logscraper.llm.client import LLMClient
    llm_client = LLMClient(resolved_settings)
    return LogScraperWorkflow(
        settings=resolved_settings,
        log_scraper=log_scraper or LogScraperAgent(resolved_settings),
        master=MasterAgent(resolved_history),
        analyzer=analyzer or DotNetAnalyzerAgent(resolved_settings),
        resolver=resolver or ResolverAgent(resolved_settings),
        reviewer=reviewer or PRReviewerAgent(llm_client=llm_client),
        repo_workspace=repo_workspace or GitHubRepoWorkspace(resolved_settings),
        history=resolved_history,
    )
