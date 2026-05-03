import json
from datetime import datetime, timezone
from pathlib import Path

from logscraper.agents.log_scraper_agent import LogScraperAgent
from logscraper.config import AppSettings
from logscraper.connectors.datadog_connector import DatadogConnector


def test_datadog_query_and_payload_shape():
    settings = AppSettings(
        datadog_service="checkout-api",
        datadog_env="prod",
        datadog_log_levels="ERROR,CRITICAL",
        datadog_lookback_minutes=15,
        max_logs_per_run=50,
    )
    connector = DatadogConnector(settings)

    assert connector.build_query() == "service:checkout-api env:prod (status:error OR status:critical)"
    payload = connector.build_search_payload(now=datetime(2026, 5, 3, tzinfo=timezone.utc))
    assert payload["page"]["limit"] == 50
    assert payload["filter"]["query"] == connector.build_query()
    assert payload["sort"] == "-timestamp"


def test_datadog_accepts_mcp_style_env_aliases(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "api-key")
    monkeypatch.setenv("DD_APP_KEY", "app-key")
    monkeypatch.delenv("DATADOG_API_KEY", raising=False)
    monkeypatch.delenv("DATADOG_APP_KEY", raising=False)

    settings = AppSettings(_env_file="does-not-exist.env")

    assert settings.datadog_api_key == "api-key"
    assert settings.datadog_app_key == "app-key"


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
