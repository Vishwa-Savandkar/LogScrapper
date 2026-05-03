import json
from datetime import datetime, timezone
from pathlib import Path

from logscraper.agents.log_scraper_agent import LogScraperAgent
from logscraper.config import AppSettings
from logscraper.connectors.datadog_connector import DatadogConnector


class FakeResponse:
    def __init__(self, body=None, *, status_code=200, headers=None):
        self._body = body or {}
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.text = "" if status_code == 202 else json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeMCPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, *, headers, json):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


class FakeHTTPError(Exception):
    def __init__(self, response):
        super().__init__("HTTP 400")
        self.response = response


class FailingClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def post(self, url, *, headers, json):
        self.calls += 1
        raise FakeHTTPError(self.response)


def test_datadog_query_and_payload_shape():
    settings = AppSettings(
        datadog_service="checkout-api",
        datadog_env="prod",
        datadog_log_levels="ERROR,CRITICAL",
        datadog_lookback_minutes=15,
        datadog_primary_query="",
        datadog_fallback_query="",
        max_logs_per_run=50,
    )
    connector = DatadogConnector(settings)

    assert connector.build_query() == "service:checkout-api env:prod (status:error OR status:critical)"
    payload = connector.build_search_payload(now=datetime(2026, 5, 3, tzinfo=timezone.utc))
    assert payload["page"]["limit"] == 50
    assert payload["filter"]["query"] == connector.build_query()
    assert payload["sort"] == "-timestamp"


def test_datadog_fetch_logs_calls_logs_api():
    settings = AppSettings(
        datadog_api_key="api-key",
        datadog_app_key="app-key",
        datadog_fetch_mode="api",
        datadog_site="datadoghq.eu",
        datadog_service="checkout-api",
        datadog_env="prod",
        datadog_primary_query="",
        datadog_fallback_query="",
        datadog_initial_lookback_days=0,
        datadog_expanded_lookback_days=0,
        max_logs_per_run=25,
    )
    connector = DatadogConnector(settings)
    log_payload = {
        "id": "abc-123",
        "type": "log",
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "checkout-api",
            "status": "error",
        },
    }
    client = FakeMCPClient(
        [
            FakeResponse(
                {
                    "data": [log_payload],
                    "meta": {"page": {}},
                }
            )
        ]
    )

    logs = connector._fetch_logs_with_api_client(client)

    assert logs == [log_payload]
    request = client.requests[0]
    assert request["url"] == "https://api.datadoghq.eu/api/v2/logs/events/search"
    assert request["headers"]["DD-API-KEY"] == "api-key"
    assert request["headers"]["DD-APPLICATION-KEY"] == "app-key"
    assert request["json"]["filter"]["query"] == "service:checkout-api env:prod (status:error OR status:critical OR status:fatal)"


def test_datadog_api_uses_prompt_style_primary_fallback_and_expanded_window():
    settings = AppSettings(
        datadog_api_key="api-key",
        datadog_app_key="app-key",
        datadog_fetch_mode="api",
        datadog_site="datadoghq.eu",
        datadog_primary_query="service:basecone.test17.matching.processor.worker error",
        datadog_fallback_query=(
            'service:basecone.test17.matching.processor.worker '
            '("[ERR]" OR error OR exception OR failed OR fatal OR (*stack* AND *trace*))'
        ),
        datadog_initial_lookback_days=7,
        datadog_expanded_lookback_days=30,
        max_logs_per_run=2,
    )
    connector = DatadogConnector(settings)
    primary_log = {
        "id": "primary",
        "type": "log",
        "attributes": {"message": "primary error"},
    }
    fallback_log = {
        "id": "fallback",
        "type": "log",
        "attributes": {"message": "[ERR] fallback"},
    }
    client = FakeMCPClient(
        [
            FakeResponse({"data": [primary_log], "meta": {"page": {}}}),
            FakeResponse({"data": [fallback_log], "meta": {"page": {}}}),
        ]
    )

    logs = connector._fetch_logs_with_api_client(client)

    assert logs == [primary_log, fallback_log]
    assert client.requests[0]["json"]["filter"]["query"] == "service:basecone.test17.matching.processor.worker error"
    assert client.requests[1]["json"]["filter"]["query"] == (
        'service:basecone.test17.matching.processor.worker '
        '("[ERR]" OR error OR exception OR failed OR fatal OR (*stack* AND *trace*))'
    )
    assert client.requests[0]["json"]["page"]["limit"] == 2


def test_datadog_api_merges_queries_before_taking_latest_logs():
    settings = AppSettings(
        datadog_api_key="api-key",
        datadog_app_key="app-key",
        datadog_fetch_mode="api",
        datadog_site="datadoghq.eu",
        datadog_primary_query="service:worker error",
        datadog_fallback_query='service:worker ("[ERR]" OR exception OR failed)',
        datadog_initial_lookback_days=1,
        datadog_expanded_lookback_days=0,
        max_logs_per_run=2,
    )
    connector = DatadogConnector(settings)
    older_primary = {
        "id": "older-primary",
        "type": "log",
        "attributes": {"timestamp": "2026-05-02T09:00:00Z", "message": "older primary"},
    }
    newer_primary = {
        "id": "newer-primary",
        "type": "log",
        "attributes": {"timestamp": "2026-05-02T10:00:00Z", "message": "newer primary"},
    }
    newest_fallback = {
        "id": "newest-fallback",
        "type": "log",
        "attributes": {"timestamp": "2026-05-03T10:00:00Z", "message": "[ERR] newest fallback"},
    }
    client = FakeMCPClient(
        [
            FakeResponse({"data": [older_primary, newer_primary], "meta": {"page": {}}}),
            FakeResponse({"data": [newest_fallback], "meta": {"page": {}}}),
        ]
    )

    logs = connector._fetch_logs_with_api_client(client)

    assert [log["id"] for log in logs] == ["newest-fallback", "newer-primary"]
    assert client.requests[0]["json"]["filter"]["query"] == "service:worker error"
    assert client.requests[1]["json"]["filter"]["query"] == 'service:worker ("[ERR]" OR exception OR failed)'


def test_datadog_logs_api_does_not_retry_bad_query_response():
    response = FakeResponse({"errors": ["invalid query"]}, status_code=400)
    response.text = '{"errors":["invalid query"]}'
    client = FailingClient(response)
    settings = AppSettings(datadog_api_key="api-key", datadog_app_key="app-key")
    connector = DatadogConnector(settings)

    try:
        connector._post_with_retries(
            client,
            "https://api.datadoghq.eu/api/v2/logs/events/search",
            {},
            {"filter": {"query": "bad query"}},
            request_name="Datadog Logs API request",
        )
    except FakeHTTPError:
        pass
    else:
        raise AssertionError("Expected bad query response to raise")

    assert client.calls == 1


def test_datadog_builds_mcp_log_search_arguments_from_tool_schema():
    settings = AppSettings(
        datadog_service="checkout-api",
        datadog_env="prod",
        datadog_log_levels="ERROR,CRITICAL",
        datadog_lookback_minutes=15,
        datadog_primary_query="",
        datadog_fallback_query="",
        datadog_initial_lookback_days=0,
        datadog_expanded_lookback_days=0,
        max_logs_per_run=50,
    )
    connector = DatadogConnector(settings)

    arguments = connector.build_log_search_arguments(
        {
            "properties": {
                "query": {"type": "string"},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "limit": {"type": "integer"},
                "max_tokens": {"type": "integer"},
            }
        },
        now=datetime(2026, 5, 3, tzinfo=timezone.utc),
    )

    assert arguments["query"] == "service:checkout-api env:prod (status:error OR status:critical)"
    assert arguments["from"] == "2026-05-02T23:45:00+00:00"
    assert arguments["to"] == "2026-05-03T00:00:00+00:00"
    assert arguments["limit"] == 50
    assert arguments["max_tokens"] == 6000


def test_datadog_fetch_logs_calls_mcp_search_tool():
    log_payload = {
        "id": "abc-123",
        "type": "log",
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "checkout-api",
            "status": "error",
        },
    }
    settings = AppSettings(
        datadog_api_key="api-key",
        datadog_app_key="app-key",
        datadog_mcp_url="https://mcp.datadoghq.eu/api/unstable/mcp-server/mcp",
        datadog_service="checkout-api",
        datadog_env="prod",
        datadog_primary_query="",
        datadog_fallback_query="",
        max_logs_per_run=25,
    )
    connector = DatadogConnector(settings)
    client = FakeMCPClient(
        [
            FakeResponse(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
                headers={"content-type": "application/json", "Mcp-Session-Id": "session-1"},
            ),
            FakeResponse(status_code=202),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {
                                "name": "search_datadog_logs",
                                "inputSchema": {
                                    "properties": {
                                        "query": {"type": "string"},
                                        "from": {"type": "string"},
                                        "to": {"type": "string"},
                                        "limit": {"type": "integer"},
                                    }
                                },
                            }
                        ]
                    },
                }
            ),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "structuredContent": {
                            "data": [log_payload],
                        }
                    },
                }
            ),
        ]
    )

    logs = connector._fetch_logs_with_mcp_client(client)

    assert logs == [log_payload]
    assert [request["json"]["method"] for request in client.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert client.requests[0]["headers"]["DD_API_KEY"] == "api-key"
    assert client.requests[0]["headers"]["DD_APPLICATION_KEY"] == "app-key"
    assert "Mcp-Session-Id" not in client.requests[0]["headers"]
    assert client.requests[1]["headers"]["Mcp-Session-Id"] == "session-1"
    call = client.requests[-1]
    assert call["headers"]["Mcp-Method"] == "tools/call"
    assert call["headers"]["Mcp-Name"] == "search_datadog_logs"
    assert call["json"]["params"]["name"] == "search_datadog_logs"
    assert "service:checkout-api" in call["json"]["params"]["arguments"]["query"]


def test_datadog_fetch_logs_loads_logs_skill_when_search_tool_is_hidden():
    log_payload = {
        "id": "abc-123",
        "type": "log",
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "checkout-api",
            "status": "error",
        },
    }
    settings = AppSettings(
        datadog_api_key="api-key",
        datadog_app_key="app-key",
        datadog_mcp_url="https://mcp.datadoghq.eu/api/unstable/mcp-server/mcp?toolsets=all",
        datadog_service="checkout-api",
        datadog_env="prod",
        datadog_primary_query="",
        datadog_fallback_query="",
        max_logs_per_run=25,
    )
    connector = DatadogConnector(settings)
    client = FakeMCPClient(
        [
            FakeResponse(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
                headers={"content-type": "application/json", "Mcp-Session-Id": "session-1"},
            ),
            FakeResponse(status_code=202),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {
                                "name": "load_datadog_skill",
                                "inputSchema": {
                                    "properties": {
                                        "skill_name": {"type": "string"},
                                        "telemetry": {"type": "object"},
                                    }
                                },
                            }
                        ]
                    },
                }
            ),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {"content": [{"type": "text", "text": "loaded"}]},
                }
            ),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "result": {
                        "structuredContent": {
                            "data": [log_payload],
                        }
                    },
                }
            ),
        ]
    )

    logs = connector._fetch_logs_with_mcp_client(client)

    assert logs == [log_payload]
    calls = [request["json"] for request in client.requests if request["json"]["method"] == "tools/call"]
    assert calls[0]["params"]["name"] == "load_datadog_skill"
    assert calls[0]["params"]["arguments"]["skill_name"] == "datadog/logs"
    assert calls[1]["params"]["name"] == "search_datadog_logs"
    assert "service:checkout-api" in calls[1]["params"]["arguments"]["query"]


def test_datadog_fetch_logs_explains_empty_mcp_tool_list():
    settings = AppSettings(datadog_api_key="api-key", datadog_app_key="app-key")
    connector = DatadogConnector(settings)

    try:
        connector._find_log_search_tool([])
    except RuntimeError as exc:
        assert "returned no tools" in str(exc)
        assert "MCP Read permission" in str(exc)
    else:
        raise AssertionError("Expected an empty MCP tool list to raise")


def test_datadog_mcp_permission_error_mentions_vs_code_auth_difference():
    settings = AppSettings(datadog_api_key="api-key", datadog_app_key="app-key")
    connector = DatadogConnector(settings)

    try:
        connector._raise_if_tool_result_error(
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "Tool not available: missing MCP Read (mcp_read), MCP Write (mcp_write).",
                    }
                ],
            },
            "search_datadog_logs",
        )
    except RuntimeError as exc:
        assert "VS Code extension" in str(exc)
        assert "app-key owner" in str(exc)
    else:
        raise AssertionError("Expected an MCP permission error to raise")


def test_datadog_auto_mode_can_fallback_to_api():
    settings = AppSettings(
        datadog_api_key="api-key",
        datadog_app_key="app-key",
        datadog_fetch_mode="auto",
        datadog_site="datadoghq.eu",
        datadog_service="checkout-api",
    )
    connector = DatadogConnector(settings)

    assert connector._can_fallback_to_api(
        RuntimeError("Datadog MCP tool search_datadog_logs failed: missing mcp_read")
    )
    assert not connector._can_fallback_to_api(RuntimeError("some unrelated failure"))


def test_datadog_accepts_mcp_style_env_aliases(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "api-key")
    monkeypatch.setenv("DD_APP_KEY", "app-key")
    monkeypatch.delenv("DATADOG_API_KEY", raising=False)
    monkeypatch.delenv("DATADOG_APP_KEY", raising=False)
    monkeypatch.delenv("DD_APPLICATION_KEY", raising=False)

    settings = AppSettings(_env_file="does-not-exist.env")

    assert settings.datadog_api_key == "api-key"
    assert settings.datadog_app_key == "app-key"


def test_datadog_accepts_dd_application_key_alias(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "api-key")
    monkeypatch.setenv("DD_APPLICATION_KEY", "application-key")
    monkeypatch.delenv("DATADOG_API_KEY", raising=False)
    monkeypatch.delenv("DATADOG_APP_KEY", raising=False)
    monkeypatch.delenv("DD_APP_KEY", raising=False)

    settings = AppSettings(_env_file="does-not-exist.env")

    assert settings.datadog_api_key == "api-key"
    assert settings.datadog_app_key == "application-key"


def test_datadog_prefers_mcp_style_env_aliases(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "mcp-api-key")
    monkeypatch.setenv("DD_APP_KEY", "mcp-app-key")
    monkeypatch.setenv("DATADOG_API_KEY", "rest-api-key")
    monkeypatch.setenv("DATADOG_APP_KEY", "rest-app-key")

    settings = AppSettings(_env_file="does-not-exist.env")

    assert settings.datadog_api_key == "mcp-api-key"
    assert settings.datadog_app_key == "mcp-app-key"


def test_datadog_mcp_server_url_is_derived_from_site():
    settings = AppSettings(datadog_site="datadoghq.eu")

    assert settings.datadog_mcp_server_url == "https://mcp.datadoghq.eu/api/unstable/mcp-server/mcp?toolsets=all"


def test_datadog_mcp_server_url_accepts_full_url_in_site_for_local_recovery():
    url = "https://mcp.datadoghq.eu/api/unstable/mcp-server/mcp"
    settings = AppSettings(datadog_site=url)

    assert settings.datadog_mcp_server_url == f"{url}?toolsets=all"


def test_datadog_mcp_server_url_keeps_explicit_toolsets():
    url = "https://mcp.datadoghq.eu/api/unstable/mcp-server/mcp?toolsets=core"
    settings = AppSettings(datadog_mcp_url=url)

    assert settings.datadog_mcp_server_url == url


def test_datadog_mcp_verify_uses_ca_bundle_when_configured():
    settings = AppSettings(datadog_mcp_ca_bundle="C:/certs/corporate-ca.pem", datadog_mcp_verify_ssl=False)
    connector = DatadogConnector(settings)

    assert connector._httpx_verify() == "C:/certs/corporate-ca.pem"


def test_datadog_mcp_verify_can_be_disabled():
    settings = AppSettings(datadog_mcp_verify_ssl=False)
    connector = DatadogConnector(settings)

    assert connector._httpx_verify() is False


def test_log_scraper_agent_parses_datadog_dotnet_payload():
    fixture = Path("tests/fixtures/sample_datadog_dotnet_logs.json")
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    settings = AppSettings(datadog_service="checkout-api", datadog_env="prod")

    events = LogScraperAgent(settings).events_from_payloads(payload["data"])

    assert len(events) == 1
    event = events[0]
    assert event.service == "checkout-api"
    assert event.environment == "prod"
    assert event.exception_type == "System.NullReferenceException"
    assert event.frames[0].class_name == "OrderController"
    assert len(event.fingerprint) == 64


def test_log_scraper_agent_keeps_status_info_when_message_infers_error():
    settings = AppSettings(
        datadog_log_levels="ERROR,CRITICAL,FATAL",
        datadog_include_inferred_errors=True,
    )
    payload = {
        "attributes": {
            "timestamp": "2026-05-03T02:30:00Z",
            "service": "basecone.test17.matching.processor.worker",
            "status": "info",
            "message": (
                "[15:41:18 ERR] Error matching customer on address for document: "
                "6e68ef12-0244-423e-9141-c0d8a8352726"
            ),
        }
    }

    events = LogScraperAgent(settings).events_from_payloads([payload])

    assert len(events) == 1
    assert events[0].log_level == "INFO"
    assert events[0].inferred_severity == "ERROR"
    assert events[0].document_id == "6e68ef12-0244-423e-9141-c0d8a8352726"
