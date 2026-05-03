from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logscraper.config import AppSettings
from logscraper.connectors.datadog_connector import DatadogConnector
from logscraper.dotnet.stacktrace_parser import DotNetStackTraceParser
from logscraper.fingerprint import fingerprint_error
from logscraper.models import LogEvent


class LogScraperAgent:
    def __init__(
        self,
        settings: AppSettings,
        connector: DatadogConnector | None = None,
        parser: DotNetStackTraceParser | None = None,
    ) -> None:
        self.settings = settings
        self.connector = connector or DatadogConnector(settings)
        self.parser = parser or DotNetStackTraceParser()

    def fetch_events(self) -> list[LogEvent]:
        return self.events_from_payloads(self.connector.fetch_logs())

    def events_from_payloads(self, payloads: list[dict[str, Any]]) -> list[LogEvent]:
        events = [self.event_from_payload(payload) for payload in payloads]
        allowed_levels = {level.upper() for level in self.settings.datadog_log_levels}
        return [event for event in events if event.log_level.upper() in allowed_levels]

    def event_from_payload(self, payload: dict[str, Any]) -> LogEvent:
        attributes = payload.get("attributes", {}) if isinstance(payload, dict) else {}
        custom = attributes.get("attributes", {}) if isinstance(attributes.get("attributes"), dict) else {}
        merged = {**attributes, **custom}

        exception_type = self._first_present(
            merged,
            "exception.type",
            "error.kind",
            "error.type",
            "ExceptionType",
            "exceptionType",
        )
        message = self._first_present(
            merged,
            "exception.message",
            "error.message",
            "message",
            "Message",
        ) or ""
        stack_trace = self._first_present(
            merged,
            "exception.stacktrace",
            "exception.stack_trace",
            "error.stack",
            "stack_trace",
            "StackTrace",
        ) or ""

        parsed = self.parser.parse("\n".join(part for part in [f"{exception_type}: {message}" if exception_type else message, stack_trace] if part))
        exception_type = exception_type or parsed.exception_type
        message = message or parsed.message
        stack_trace = stack_trace or parsed.stack_trace
        frames = parsed.frames if parsed.frames else self.parser.parse_stack_trace(stack_trace)

        fingerprint = fingerprint_error(
            exception_type=exception_type,
            message=message,
            stack_trace=stack_trace,
            frames=frames,
        )

        return LogEvent(
            timestamp=self._parse_timestamp(attributes.get("timestamp") or merged.get("timestamp")),
            service=self._first_present(merged, "service", "Service") or self.settings.datadog_service or None,
            environment=self._first_present(merged, "env", "environment", "Environment") or self.settings.datadog_env,
            log_level=str(self._first_present(merged, "status", "level", "LogLevel") or "ERROR").upper(),
            exception_type=exception_type,
            error_message=message,
            stack_trace=stack_trace,
            fingerprint=fingerprint,
            trace_id=self._first_present(merged, "dd.trace_id", "trace_id", "TraceId"),
            span_id=self._first_present(merged, "dd.span_id", "span_id", "SpanId"),
            raw_payload=payload,
            frames=frames,
        )

    def _first_present(self, payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value)
        return None

    def _parse_timestamp(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if value:
            normalized = str(value).replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                pass
        return datetime.now(timezone.utc)
