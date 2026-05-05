from __future__ import annotations

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
        }
        if initial_state:
            state.update(initial_state)

        graph = self.to_langgraph()
        if graph is not None:
            try:
                return graph.invoke(state)
            except Exception as exc:
                state.setdefault("errors", []).append(str(exc))
                return state

        try:
            if not state["log_events"]:
                state["log_events"] = self.fetch_datadog_logs()
            state = self.route_error(state)
            if not state.get("resolution_task"):
                return state
            state = self.prepare_repo_workspace(state)
            state = self.analyze_dotnet_code(state)
            state = self.plan_fix(state)
            state = self.apply_or_dry_run_fix(state)
            state = self.review_fix(state)
            state = self.update_history(state)
        except Exception as exc:
            state.setdefault("errors", []).append(str(exc))
        return state

    def to_langgraph(self):
        try:
            from langgraph.graph import END, START, StateGraph
        except ModuleNotFoundError as exc:
            if exc.name == "langgraph" or str(exc.name).startswith("langgraph."):
                return None
            raise

        graph = StateGraph(AgentState)
        graph.add_node("fetch_datadog_logs", self._fetch_node)
        graph.add_node("deduplicate_errors", self._deduplicate_node)
        graph.add_node("route_error", self.route_error)
        graph.add_node("prepare_repo_workspace", self.prepare_repo_workspace)
        graph.add_node("analyze_dotnet_code", self.analyze_dotnet_code)
        graph.add_node("plan_fix", self.plan_fix)
        graph.add_node("apply_or_dry_run_fix", self.apply_or_dry_run_fix)
        graph.add_node("review_fix", self.review_fix)
        graph.add_node("update_history", self.update_history)

        graph.add_edge(START, "fetch_datadog_logs")
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
        graph.add_edge("analyze_dotnet_code", "plan_fix")
        graph.add_edge("plan_fix", "apply_or_dry_run_fix")
        graph.add_edge("apply_or_dry_run_fix", "review_fix")
        graph.add_edge("review_fix", "update_history")
        graph.add_edge("update_history", END)
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

    def _fetch_node(self, state: AgentState) -> AgentState:
        print("[workflow] fetch_datadog_logs started")
        if state.get("log_events"):
            print(f"[workflow] fetch_datadog_logs skipped; existing_events={len(state['log_events'])}")
            return state
        log_events = self.fetch_datadog_logs()
        print(f"[workflow] fetch_datadog_logs completed; events={len(log_events)}")
        self._print_fetched_events(log_events)
        return {**state, "log_events": log_events}

    def _deduplicate_node(self, state: AgentState) -> AgentState:
        print(f"[workflow] deduplicate_errors started; events={len(state.get('log_events', []))}")
        seen: set[str] = set()
        unique: list[LogEvent] = []
        for event in state.get("log_events", []):
            if event.fingerprint in seen:
                continue
            seen.add(event.fingerprint)
            unique.append(event)
        print(f"[workflow] deduplicate_errors completed; unique_events={len(unique)}")
        return {**state, "log_events": unique}

    def _route_after_routing(self, state: AgentState) -> str:
        return "actionable" if state.get("resolution_task") else "done"

    def fetch_datadog_logs(self) -> list[LogEvent]:
        return self.log_scraper.fetch_events()

    def _print_fetched_events(self, events: list[LogEvent]) -> None:
        for index, event in enumerate(events, start=1):
            print(
                "[workflow] fetched_event "
                f"#{index} timestamp={event.timestamp.isoformat()} "
                f"status={event.log_level} inferred={event.inferred_severity} "
                f"service={event.service} document_id={event.document_id} "
                f"fingerprint={event.fingerprint} "
                f"message={self._truncate(event.error_message, limit=300)}"
            )

    def _truncate(self, value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def route_error(self, state: AgentState) -> AgentState:
        print(f"[workflow] route_error started; events={len(state.get('log_events', []))}")
        for event in state.get("log_events", []):
            route, task = self.master.route_event(event)
            state["route"] = route
            print(f"[workflow] route_error checked; route={route} fingerprint={event.fingerprint}")
            if task:
                state["current_event"] = event
                state["resolution_task"] = task
                self.history.update_status(task.fingerprint, ErrorStatus.IN_ANALYSIS)
                print(f"[workflow] route_error completed; selected_fingerprint={task.fingerprint}")
                return state
        print("[workflow] route_error completed; no actionable task")
        return state

    def prepare_repo_workspace(self, state: AgentState) -> AgentState:
        print("[workflow] prepare_repo_workspace started")
        task = state.get("resolution_task")
        if task is None:
            print("[workflow] prepare_repo_workspace skipped; no resolution task")
            return state

        if self.settings.github_token and self.settings.github_repository:
            repo_path = self.repo_workspace.prepare(task.fingerprint)
            state["repo_path"] = str(repo_path)
            print(f"[workflow] prepare_repo_workspace completed; repo_path={repo_path}")
            return state

        local_repo = self.settings.dotnet_repo
        if local_repo:
            state["repo_path"] = str(local_repo)
            print(f"[workflow] prepare_repo_workspace completed; local_repo_path={local_repo}")
            return state

        print("[workflow] prepare_repo_workspace completed; no GitHub or local repo configured")
        return state

    def analyze_dotnet_code(self, state: AgentState) -> AgentState:
        print("[workflow] analyze_dotnet_code started")
        task = state.get("resolution_task")
        if task is None:
            print("[workflow] analyze_dotnet_code skipped; no resolution task")
            return state
        state["code_fix_plan"] = self.analyzer.analyze(task, repo_path=state.get("repo_path"))
        print(f"[workflow] analyze_dotnet_code completed; fingerprint={task.fingerprint}")
        return state

    def plan_fix(self, state: AgentState) -> AgentState:
        print("[workflow] plan_fix started")
        task = state.get("resolution_task")
        if task is not None:
            self.history.update_status(task.fingerprint, ErrorStatus.FIX_PLANNED)
            print(f"[workflow] plan_fix completed; fingerprint={task.fingerprint}")
        else:
            print("[workflow] plan_fix skipped; no resolution task")
        return state

    def apply_or_dry_run_fix(self, state: AgentState) -> AgentState:
        print("[workflow] apply_or_dry_run_fix started")
        task = state.get("resolution_task")
        plan = state.get("code_fix_plan")
        if task is None or plan is None:
            print("[workflow] apply_or_dry_run_fix skipped; missing task or plan")
            return state
        state["pull_request_result"] = self.resolver.prepare_pull_request(task, plan)
        result = state["pull_request_result"]
        print(
            "[workflow] apply_or_dry_run_fix completed; "
            f"dry_run={result.dry_run} branch={result.branch_name} pr_url={result.pr_url}"
        )
        return state

    def review_fix(self, state: AgentState) -> AgentState:
        print("[workflow] review_fix started")
        plan = state.get("code_fix_plan")
        result = state.get("pull_request_result")
        if plan is None or result is None:
            print("[workflow] review_fix skipped; missing plan or pull request result")
            return state
        state["review_result"] = self.reviewer.review(plan, result)
        print("[workflow] review_fix completed")
        return state

    def update_history(self, state: AgentState) -> AgentState:
        print("[workflow] update_history started")
        task = state.get("resolution_task")
        result = state.get("pull_request_result")
        if task is None or result is None:
            print("[workflow] update_history skipped; missing task or pull request result")
            return state
        if result.pr_url:
            self.history.attach_pr(task.fingerprint, result.pr_url, status=ErrorStatus.IN_REVIEW)
            print(f"[workflow] update_history completed; pr_url={result.pr_url}")
        else:
            self.history.update_status(task.fingerprint, ErrorStatus.FIX_PLANNED)
            print(f"[workflow] update_history completed; status={ErrorStatus.FIX_PLANNED.value}")
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
    return LogScraperWorkflow(
        settings=resolved_settings,
        log_scraper=log_scraper or LogScraperAgent(resolved_settings),
        master=MasterAgent(resolved_history),
        analyzer=analyzer or DotNetAnalyzerAgent(resolved_settings),
        resolver=resolver or ResolverAgent(resolved_settings),
        reviewer=reviewer or PRReviewerAgent(),
        repo_workspace=repo_workspace or GitHubRepoWorkspace(resolved_settings),
        history=resolved_history,
    )
