from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from logscraper.agents.dotnet_analyzer_agent import DotNetAnalyzerAgent
from logscraper.agents.log_scraper_agent import LogScraperAgent
from logscraper.agents.master_agent import MasterAgent
from logscraper.agents.pr_reviewer_agent import PRReviewerAgent
from logscraper.agents.resolver_agent import ResolverAgent
from logscraper.config import AppSettings
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
        history: HistoryDB,
    ) -> None:
        self.settings = settings
        self.log_scraper = log_scraper
        self.master = master
        self.analyzer = analyzer
        self.resolver = resolver
        self.reviewer = reviewer
        self.history = history

    def run(self, initial_state: AgentState | None = None) -> AgentState:
        state: AgentState = {
            "log_events": [],
            "current_event": None,
            "resolution_task": None,
            "code_fix_plan": None,
            "pull_request_result": None,
            "review_result": None,
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
                "actionable": "analyze_dotnet_code",
                "done": END,
            },
        )
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
        if state.get("log_events"):
            return state
        return {**state, "log_events": self.fetch_datadog_logs()}

    def _deduplicate_node(self, state: AgentState) -> AgentState:
        seen: set[str] = set()
        unique: list[LogEvent] = []
        for event in state.get("log_events", []):
            if event.fingerprint in seen:
                continue
            seen.add(event.fingerprint)
            unique.append(event)
        return {**state, "log_events": unique}

    def _route_after_routing(self, state: AgentState) -> str:
        return "actionable" if state.get("resolution_task") else "done"

    def fetch_datadog_logs(self) -> list[LogEvent]:
        return self.log_scraper.fetch_events()

    def route_error(self, state: AgentState) -> AgentState:
        for event in state.get("log_events", []):
            route, task = self.master.route_event(event)
            state["route"] = route
            if task:
                state["current_event"] = event
                state["resolution_task"] = task
                self.history.update_status(task.fingerprint, ErrorStatus.IN_ANALYSIS)
                return state
        return state

    def analyze_dotnet_code(self, state: AgentState) -> AgentState:
        task = state.get("resolution_task")
        if task is None:
            return state
        state["code_fix_plan"] = self.analyzer.analyze(task)
        return state

    def plan_fix(self, state: AgentState) -> AgentState:
        task = state.get("resolution_task")
        if task is not None:
            self.history.update_status(task.fingerprint, ErrorStatus.FIX_PLANNED)
        return state

    def apply_or_dry_run_fix(self, state: AgentState) -> AgentState:
        task = state.get("resolution_task")
        plan = state.get("code_fix_plan")
        if task is None or plan is None:
            return state
        state["pull_request_result"] = self.resolver.prepare_pull_request(task, plan)
        return state

    def review_fix(self, state: AgentState) -> AgentState:
        plan = state.get("code_fix_plan")
        result = state.get("pull_request_result")
        if plan is None or result is None:
            return state
        state["review_result"] = self.reviewer.review(plan, result)
        return state

    def update_history(self, state: AgentState) -> AgentState:
        task = state.get("resolution_task")
        result = state.get("pull_request_result")
        if task is None or result is None:
            return state
        if result.pr_url:
            self.history.attach_pr(task.fingerprint, result.pr_url, status=ErrorStatus.IN_REVIEW)
        else:
            self.history.update_status(task.fingerprint, ErrorStatus.FIX_PLANNED)
        return state


def build_workflow(
    *,
    settings: AppSettings | None = None,
    log_scraper: LogScraperAgent | None = None,
    history: HistoryDB | None = None,
    analyzer: DotNetAnalyzerAgent | None = None,
    resolver: ResolverAgent | None = None,
    reviewer: PRReviewerAgent | None = None,
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
        history=resolved_history,
    )
