from pathlib import Path

from logscraper.agents.log_scraper_agent import LogScraperAgent
from logscraper.config import AppSettings
from logscraper.graph.workflow import build_workflow
from logscraper.models import CodeFixPlan
from logscraper.store.history_db import HistoryDB


class FakeLogScraper:
    def __init__(self, events):
        self.events = events

    def fetch_events(self):
        return self.events


class FakeRepoWorkspace:
    def __init__(self, path: Path):
        self.path = path
        self.fingerprint = None

    def prepare(self, fingerprint: str):
        self.fingerprint = fingerprint
        return self.path


class FakeAnalyzer:
    def __init__(self):
        self.repo_path = None

    def analyze(self, task, repo_path=None):
        self.repo_path = repo_path
        return CodeFixPlan(
            fingerprint=task.fingerprint,
            root_cause_summary="Fake root cause",
            suggested_fix="Fake suggested fix",
            confidence=0.75,
        )


def make_settings(tmp_path: Path, **overrides):
    values = {
        "_env_file": str(tmp_path / "missing.env"),
        "db_path": str(tmp_path / "history.db"),
        "dry_run": True,
        "github_token": "",
        "github_repo_owner": "",
        "github_repo_name": "",
        "openai_api_key": "",
        "dotnet_repo_path": "",
    }
    values.update(overrides)
    return AppSettings(**values)


def test_workflow_routes_new_error_to_dry_run_pr(tmp_path: Path):
    settings = make_settings(tmp_path)
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


def test_workflow_prepares_github_repo_before_analysis(tmp_path: Path):
    settings = make_settings(
        tmp_path,
        github_token="fake-token",
        github_repo_owner="example",
        github_repo_name="checkout-api",
    )
    raw = {
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "checkout-api",
            "status": "error",
            "attributes": {
                "env": "prod",
                "exception.type": "System.InvalidOperationException",
                "exception.message": "Example failure.",
            },
        }
    }
    event = LogScraperAgent(settings).event_from_payload(raw)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_workspace = FakeRepoWorkspace(repo_path)
    analyzer = FakeAnalyzer()
    workflow = build_workflow(
        settings=settings,
        log_scraper=FakeLogScraper([event]),
        history=HistoryDB(tmp_path / "history.db"),
        analyzer=analyzer,
        repo_workspace=repo_workspace,
    )

    state = workflow.run()

    assert state["repo_path"] == str(repo_path)
    assert repo_workspace.fingerprint == event.fingerprint
    assert analyzer.repo_path == str(repo_path)


def test_workflow_prints_nodes_and_fetched_events(tmp_path: Path, capsys):
    settings = make_settings(tmp_path)
    raw = {
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "checkout-api",
            "status": "info",
            "message": "[ERR] Error matching customer on address for document: 6e68ef12-0244-423e-9141-c0d8a8352726",
        }
    }
    event = LogScraperAgent(settings).event_from_payload(raw)
    workflow = build_workflow(
        settings=settings,
        log_scraper=FakeLogScraper([event]),
        history=HistoryDB(tmp_path / "history.db"),
    )

    workflow.run()

    output = capsys.readouterr().out
    assert "[workflow] fetch_datadog_logs started" in output
    assert "[workflow] fetched_event #1" in output
    assert "status=INFO" in output
    assert "inferred=ERROR" in output
    assert "document_id=6e68ef12-0244-423e-9141-c0d8a8352726" in output
    assert "[workflow] route_error started" in output


def test_workflow_draws_mermaid_graph(tmp_path: Path):
    settings = make_settings(tmp_path)
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
