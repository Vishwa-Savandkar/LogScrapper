from pathlib import Path

from logscraper.agents.log_scraper_agent import LogScraperAgent
from logscraper.config import AppSettings
from logscraper.graph.workflow import build_workflow
from logscraper.store.history_db import HistoryDB


class FakeLogScraper:
    def __init__(self, events):
        self.events = events

    def fetch_events(self):
        return self.events


def test_workflow_routes_new_error_to_dry_run_pr(tmp_path: Path):
    settings = AppSettings(db_path=str(tmp_path / "history.db"), dry_run=True)
    raw = {
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "checkout-api",
            "status": "error",
            "attributes": {
                "env": "prod",
                "exception.type": "System.NullReferenceException",
                "exception.message": "Object reference not set to an instance of an object.",
                "exception.stacktrace": "   at Checkout.Api.Controllers.OrderController.Get(Int32 id) in C:\\src\\OrderController.cs:line 42",
            },
        }
    }
    event = LogScraperAgent(settings).event_from_payload(raw)
    history = HistoryDB(tmp_path / "history.db")
    workflow = build_workflow(
        settings=settings,
        log_scraper=FakeLogScraper([event]),
        history=history,
    )

    state = workflow.run()

    assert state["route"] == "actionable"
    assert state["code_fix_plan"] is not None
    assert state["pull_request_result"].dry_run is True
    assert state["pull_request_result"].branch_name.startswith("logscraper/nullreferenceexception-")
    assert history.get_record(event.fingerprint).status.value == "fix_planned"

    duplicate_state = workflow.run()
    assert duplicate_state["route"] == "duplicate"
    assert duplicate_state["resolution_task"] is None


def test_workflow_draws_mermaid_graph(tmp_path: Path):
    settings = AppSettings(db_path=str(tmp_path / "history.db"), dry_run=True)
    workflow = build_workflow(
        settings=settings,
        log_scraper=FakeLogScraper([]),
        history=HistoryDB(tmp_path / "history.db"),
    )

    graph = workflow.draw_mermaid()

    assert "fetch_datadog_logs" in graph
    assert "route_error" in graph
    assert "analyze_dotnet_code" in graph

    path = tmp_path / "workflow-graph.mmd"
    assert workflow.write_graph(path) == path
    assert path.read_text(encoding="utf-8") == graph
