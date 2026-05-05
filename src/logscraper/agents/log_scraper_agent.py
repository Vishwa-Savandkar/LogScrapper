from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from logscraper.config import AppSettings
from logscraper.connectors.datadog_connector import DatadogConnector
from logscraper.dotnet.stacktrace_parser import DotNetStackTraceParser
from logscraper.fingerprint import fingerprint_error
from logscraper.models import LogEvent


_DOCUMENT_ID_RE = re.compile(
    r"\bdocument:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


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
        return [
            event
            for event in events
            if event.log_level.upper() in allowed_levels
            or (self.settings.datadog_include_inferred_errors and event.inferred_severity == "ERROR")
        ]

    def event_from_payload(self, payload: dict[str, Any]) -> LogEvent:
        attributes = self._dict_value(payload.get("attributes")) if isinstance(payload, dict) else {}
        log_fields = self._collect_log_fields(attributes)
        merged = {**attributes, **log_fields}

        log_message = self._first_present(
            merged,
            "message",
            "Message",
            "RenderedMessage",
        ) or ""
        exception_type = self._first_present(
            merged,
            "exception.type",
            "error.kind",
            "error.type",
            "ExceptionType",
            "exceptionType",
            "Exception.Type",
        )
        exception_message = self._first_present(
            merged,
            "exception.message",
            "error.message",
            "exceptionMessage",
            "ExceptionMessage",
            "Exception.Message",
        ) or ""
        detailed_exception = self._first_present(
            merged,
            "Exception",
            "exception",
            "DetailedException",
            "detailed_exception",
            "error.exception",
        ) or ""
        explicit_stack_trace = self._first_present(
            merged,
            "exception.stacktrace",
            "exception.stack_trace",
            "error.stack",
            "error.stack_trace",
            "stack_trace",
            "StackTrace",
            "Stacktrace",
            "Stack",
        ) or ""

        exception_text = self._exception_text(
            exception_type=exception_type,
            exception_message=exception_message,
            detailed_exception=detailed_exception,
            stack_trace=explicit_stack_trace,
        )
        parsed = self.parser.parse(exception_text or log_message)
        exception_type = exception_type or parsed.exception_type
        exception_message = exception_message or self._parsed_exception_message(parsed.message, log_message)
        stack_trace = explicit_stack_trace or parsed.stack_trace
        detailed_exception = detailed_exception or self._exception_text(
            exception_type=exception_type,
            exception_message=exception_message,
            detailed_exception="",
            stack_trace=stack_trace,
        )
        message = log_message or exception_message or parsed.message
        frames = parsed.frames if parsed.frames else self.parser.parse_stack_trace(stack_trace)
        inferred_severity = self._infer_severity(message, exception_message, detailed_exception, stack_trace)

        fingerprint = fingerprint_error(
            exception_type=exception_type,
            message=exception_message or message,
            stack_trace=stack_trace,
            frames=frames,
        )

        return LogEvent(
            timestamp=self._parse_timestamp(attributes.get("timestamp") or merged.get("timestamp")),
            service=self._first_present(merged, "service", "Service") or self.settings.datadog_service or None,
            environment=self._first_present(merged, "env", "environment", "Environment") or self.settings.datadog_env,
            log_level=self._log_level(merged),
            exception_type=exception_type,
            error_message=message,
            exception_message=exception_message,
            detailed_exception=detailed_exception,
            stack_trace=stack_trace,
            fingerprint=fingerprint,
            trace_id=self._first_present(merged, "dd.trace_id", "trace_id", "TraceId"),
            span_id=self._first_present(merged, "dd.span_id", "span_id", "SpanId"),
            inferred_severity=inferred_severity,
            document_id=self._first_present(merged, "document_id", "document.id", "documentId", "DocumentId")
            or self._extract_document_id(
                message,
                exception_message,
                detailed_exception,
                stack_trace,
                self._json_text(log_fields),
            ),
            log_attributes=attributes,
            log_fields=log_fields,
            raw_payload=payload,
            frames=frames,
        )

    def _first_present(self, payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = self._lookup_value(payload, key)
            if value is not None and str(value).strip():
                return self._string_value(value)
        return None

    def _lookup_value(self, payload: dict[str, Any], key: str) -> Any:
        if key in payload:
            return payload[key]
        normalized_key = key.lower()
        for existing_key, value in payload.items():
            if str(existing_key).lower() == normalized_key:
                return value
        return None

    def _dict_value(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _collect_log_fields(self, attributes: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key in (
            "attributes",
            "Attributes",
            "fields",
            "Fields",
            "properties",
            "Properties",
            "custom",
            "Custom",
        ):
            value = attributes.get(key)
            if isinstance(value, dict):
                fields.update(value)
        return fields

    def _log_level(self, payload: dict[str, Any]) -> str:
        status = self._first_present(payload, "status")
        level = self._first_present(payload, "level", "Level", "LogLevel")
        if status and status.upper() not in {"INFO", "DEBUG", "TRACE", "NOTICE"}:
            return status.upper()
        return str(level or status or "ERROR").upper()

    def _exception_text(
        self,
        *,
        exception_type: str | None,
        exception_message: str,
        detailed_exception: str,
        stack_trace: str,
    ) -> str:
        if detailed_exception:
            return detailed_exception
        header = (
            f"{exception_type}: {exception_message}"
            if exception_type and exception_message
            else exception_message
        )
        return "\n".join(part for part in [header, stack_trace] if part)

    def _parsed_exception_message(self, parsed_message: str, log_message: str) -> str:
        if "\n" in parsed_message or parsed_message.lstrip().startswith("at "):
            return ""
        if parsed_message and parsed_message != log_message:
            return parsed_message
        return ""

    def _string_value(self, value: Any) -> str:
        if isinstance(value, (dict, list)):
            return self._json_text(value)
        return str(value)

    def _json_text(self, value: Any) -> str:
        if not value:
            return ""
        return json.dumps(value, default=str, sort_keys=True)

    def _infer_severity(self, *parts: str) -> str | None:
        text = "\n".join(part for part in parts if part).lower()
        for keyword in self.settings.datadog_error_keywords:
            if keyword.lower() in text:
                return "ERROR"
        return None

    def _extract_document_id(self, *parts: str) -> str | None:
        text = "\n".join(part for part in parts if part)
        match = _DOCUMENT_ID_RE.search(text)
        return match.group(1) if match else None

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
