from logscraper import main as main_module


class FakeSettings:
    datadog_api_key = "datadog-api-key"
    datadog_app_key = "datadog-app-key"
    workflow_graph_path = "data/workflow-graph.mmd"
    workflow_graph_only = False


class FakeWorkflow:
    def __init__(self, calls):
        self.calls = calls

    def write_graph(self, path):
        self.calls.append(("write_graph", path))

    def run(self):
        self.calls.append(("run", None))
        return {
            "log_events": [],
            "current_event": None,
            "pull_request_result": None,
            "errors": [],
        }


def test_main_renders_graph_then_runs_workflow(monkeypatch):
    calls = []

    monkeypatch.setattr(main_module, "AppSettings", FakeSettings)
    monkeypatch.setattr(main_module, "build_workflow", lambda settings: FakeWorkflow(calls))

    assert main_module.main() == 0
    assert calls == [
        ("write_graph", "data/workflow-graph.mmd"),
        ("run", None),
    ]


def test_main_can_render_graph_only(monkeypatch):
    calls = []

    class GraphOnlySettings(FakeSettings):
        workflow_graph_only = True

    monkeypatch.setattr(main_module, "AppSettings", GraphOnlySettings)
    monkeypatch.setattr(main_module, "build_workflow", lambda settings: FakeWorkflow(calls))

    assert main_module.main() == 0
    assert calls == [("write_graph", "data/workflow-graph.mmd")]
