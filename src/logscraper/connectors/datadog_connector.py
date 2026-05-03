from __future__ import annotations

import json
import logging
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, TypeAlias

from logscraper.config import AppSettings


logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"
DATADOG_LOG_SEARCH_TOOL = "search_datadog_logs"
LEGACY_DATADOG_LOG_SEARCH_TOOL = "get_logs"
LIST_DATADOG_SKILLS_TOOL = "list_datadog_skills"
LOAD_DATADOG_SKILL_TOOL = "load_datadog_skill"
HTTPX_VERIFY: TypeAlias = bool | str | ssl.SSLContext


class DatadogConnector:
    def __init__(self, settings: AppSettings, *, timeout_seconds: float = 20.0) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self._request_id = 0
        self._mcp_session_id: str | None = None
        self._mcp_protocol_version = MCP_PROTOCOL_VERSION

    def build_query(self) -> str:
        if self.settings.datadog_primary_query:
            return self.settings.datadog_primary_query

        terms: list[str] = []
        if self.settings.datadog_service:
            terms.append(f"service:{self.settings.datadog_service}")
        if self.settings.datadog_env:
            terms.append(f"env:{self.settings.datadog_env}")
        if self.settings.datadog_log_levels:
            levels = " OR ".join(f"status:{level.lower()}" for level in self.settings.datadog_log_levels)
            terms.append(f"({levels})")
        return " ".join(terms) or "*"

    def build_search_payload(
        self,
        *,
        cursor: str | None = None,
        query: str | None = None,
        lookback_minutes: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        to_time = now or datetime.now(timezone.utc)
        from_time = to_time - timedelta(minutes=lookback_minutes or self._initial_lookback_minutes())
        payload: dict[str, Any] = {
            "filter": {
                "query": query or self.build_query(),
                "from": from_time.isoformat(),
                "to": to_time.isoformat(),
            },
            "page": {
                "limit": min(self.settings.max_logs_per_run, 1000),
            },
            "sort": "-timestamp",
        }
        if cursor:
            payload["page"]["cursor"] = cursor
        return payload

    def build_search_plan(self) -> list[tuple[str, int]]:
        primary_query = self.build_query()
        fallback_query = self.settings.datadog_fallback_query.strip()
        queries = [primary_query]
        if fallback_query and fallback_query != primary_query:
            queries.append(fallback_query)

        windows = [self._initial_lookback_minutes()]
        expanded_minutes = self.settings.datadog_expanded_lookback_days * 24 * 60
        if expanded_minutes > windows[0]:
            windows.append(expanded_minutes)

        return [(query, window) for window in windows for query in queries]

    def build_log_search_arguments(
        self,
        input_schema: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        to_time = now or datetime.now(timezone.utc)
        from_time = to_time - timedelta(minutes=self._initial_lookback_minutes())
        limit = min(self.settings.max_logs_per_run, 1000)
        query = self.build_query()
        properties = (input_schema or {}).get("properties") or {}

        if not properties:
            return {
                "query": query,
                "from": from_time.isoformat(),
                "to": to_time.isoformat(),
                "limit": limit,
                "sort": "-timestamp",
            }

        arguments: dict[str, Any] = {}
        for name, schema in properties.items():
            normalized = name.replace("-", "_").lower()
            value_type = schema.get("type") if isinstance(schema, dict) else None

            if normalized in {"query", "filter", "filter_query", "filterquery", "log_query", "search_query"}:
                arguments[name] = query
            elif normalized in {"from", "start", "start_time", "starttime", "filter_from", "filterfrom"}:
                arguments[name] = self._format_mcp_time(from_time, value_type)
            elif normalized in {"to", "end", "end_time", "endtime", "filter_to", "filterto"}:
                arguments[name] = self._format_mcp_time(to_time, value_type)
            elif normalized in {"limit", "page_limit", "pagelimit", "max_logs", "maxlogs"}:
                arguments[name] = limit
            elif normalized == "sort":
                arguments[name] = "-timestamp"
            elif normalized == "service" and self.settings.datadog_service:
                arguments[name] = self.settings.datadog_service
            elif normalized in {"env", "environment"} and self.settings.datadog_env:
                arguments[name] = self.settings.datadog_env
            elif normalized == "max_tokens":
                arguments[name] = 6000
            elif normalized in {"prompt", "question"}:
                arguments[name] = (
                    "Search Datadog logs using query "
                    f"`{query}` from {from_time.isoformat()} to {to_time.isoformat()}. "
                    f"Return up to {limit} log events with their attributes."
                )

        return arguments

    def fetch_logs(self) -> list[dict[str, Any]]:
        if not self.settings.datadog_api_key or not self.settings.datadog_app_key:
            raise ValueError(
                "Datadog API and app keys are required. Use DATADOG_API_KEY/DATADOG_APP_KEY or "
                "DD_API_KEY/DD_APP_KEY."
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - requires optional deps absent.
            raise RuntimeError("httpx is required for Datadog calls. Install project dependencies.") from exc

        with httpx.Client(timeout=self.timeout_seconds, verify=self._httpx_verify()) as client:
            if self.settings.datadog_fetch_mode == "api":
                return self._fetch_logs_with_api_client(client)
            if self.settings.datadog_fetch_mode == "mcp":
                return self._fetch_logs_with_mcp_client(client)
            if self.settings.datadog_fetch_mode == "auto":
                try:
                    return self._fetch_logs_with_mcp_client(client)
                except RuntimeError as exc:
                    if not self._can_fallback_to_api(exc):
                        raise
                    logger.warning(
                        "Datadog MCP fetch failed; falling back to Logs API.",
                        extra={"error": str(exc), "fallback": "api"},
                    )
                    return self._fetch_logs_with_api_client(client)
            raise ValueError("DATADOG_FETCH_MODE must be one of: api, mcp, auto.")

    def _fetch_logs_with_api_client(self, client: Any) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for query, lookback_minutes in self.build_search_plan():
            for payload in self._fetch_logs_for_query(client, query, lookback_minutes):
                event_id = str(payload.get("id") or json.dumps(payload, sort_keys=True, default=str))
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
                logs.append(payload)
                if len(logs) >= self.settings.max_logs_per_run:
                    break
            if len(logs) >= self.settings.max_logs_per_run:
                break
        return logs[: self.settings.max_logs_per_run]

    def _fetch_logs_for_query(self, client: Any, query: str, lookback_minutes: int) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        cursor: str | None = None
        url = f"{self.settings.datadog_base_url}/api/v2/logs/events/search"
        headers = {
            "DD-API-KEY": self.settings.datadog_api_key,
            "DD-APPLICATION-KEY": self.settings.datadog_app_key,
            "Content-Type": "application/json",
        }

        while len(logs) < self.settings.max_logs_per_run:
            payload = self.build_search_payload(cursor=cursor, query=query, lookback_minutes=lookback_minutes)
            response = self._post_with_retries(
                client,
                url,
                headers,
                payload,
                request_name="Datadog Logs API request",
            )
            body = response.json()
            logs.extend(body.get("data", []))
            cursor = body.get("meta", {}).get("page", {}).get("after")
            if not cursor:
                break
        return logs[: self.settings.max_logs_per_run]

    def _fetch_logs_with_mcp_client(self, client: Any) -> list[dict[str, Any]]:
        self._initialize_mcp_session(client)
        tools = self._list_mcp_tools(client)
        tool = self._find_log_search_tool(tools, allow_skill_loader=True)
        if tool is None:
            self._load_logs_skill(client)
            tool_name = DATADOG_LOG_SEARCH_TOOL
            arguments = self.build_log_search_arguments()
        else:
            input_schema = tool.get("inputSchema") if isinstance(tool, dict) else {}
            arguments = self.build_log_search_arguments(input_schema if isinstance(input_schema, dict) else {})
            tool_name = str(tool["name"])
        result = self._call_mcp_tool(client, tool_name, arguments)
        self._raise_if_tool_result_error(result, tool_name)
        logs = self._extract_logs_from_tool_result(result)
        return logs[: self.settings.max_logs_per_run]

    def _initialize_mcp_session(self, client: Any) -> None:
        result, response = self._send_mcp_request(
            client,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "logscraper",
                    "version": "0.1.0",
                },
            },
            include_id=True,
        )
        protocol_version = result.get("protocolVersion") if isinstance(result, dict) else None
        if protocol_version:
            self._mcp_protocol_version = str(protocol_version)
        self._mcp_session_id = response.headers.get("Mcp-Session-Id")
        self._send_mcp_request(client, "notifications/initialized", include_id=False)

    def _list_mcp_tools(self, client: Any) -> list[dict[str, Any]]:
        result, _response = self._send_mcp_request(client, "tools/list", include_id=True)
        tools = result.get("tools") if isinstance(result, dict) else None
        return [tool for tool in tools or [] if isinstance(tool, dict)]

    def _find_log_search_tool(
        self,
        tools: list[dict[str, Any]],
        *,
        allow_skill_loader: bool = False,
    ) -> dict[str, Any] | None:
        if not tools:
            raise RuntimeError(
                "Datadog MCP returned no tools. Check that the application key has MCP Read permission, "
                "the Datadog MCP preview/server is enabled for this org, and the endpoint/toolsets are correct."
            )
        for expected_name in (DATADOG_LOG_SEARCH_TOOL, LEGACY_DATADOG_LOG_SEARCH_TOOL):
            for tool in tools:
                if tool.get("name") == expected_name:
                    return tool
        if allow_skill_loader and any(tool.get("name") == LOAD_DATADOG_SKILL_TOOL for tool in tools):
            return None
        available = ", ".join(str(tool.get("name")) for tool in tools if tool.get("name"))
        raise RuntimeError(f"Datadog MCP log search tool was not found. Available tools: {available or 'none'}")

    def _load_logs_skill(self, client: Any) -> None:
        result = self._call_mcp_tool(
            client,
            LOAD_DATADOG_SKILL_TOOL,
            {
                "skill_name": "datadog/logs",
                "header_only": True,
                "telemetry": {
                    "intent": "Load Datadog log-search guidance before fetching recent error logs for LogScraper."
                },
            },
        )
        self._raise_if_tool_result_error(result, LOAD_DATADOG_SKILL_TOOL)

    def _call_mcp_tool(self, client: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result, _response = self._send_mcp_request(
            client,
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
            include_id=True,
            mcp_name=name,
        )
        return result if isinstance(result, dict) else {}

    def _raise_if_tool_result_error(self, result: dict[str, Any], tool_name: str) -> None:
        if not result.get("isError"):
            return
        messages = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("text"):
                messages.append(str(item["text"]))
        detail = " ".join(messages) if messages else "No error details returned."
        if "mcp_read" in detail.lower() or "mcp_write" in detail.lower():
            detail = (
                f"{detail} The standalone LogScraper app authenticates with API/app-key headers from .env or "
                "environment variables. This is separate from the Datadog VS Code extension's signed-in/OAuth "
                "session, so VS Code can work while this app still needs MCP permissions on the app-key owner."
            )
        raise RuntimeError(f"Datadog MCP tool {tool_name} failed: {detail}")

    def _send_mcp_request(
        self,
        client: Any,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        include_id: bool,
        mcp_name: str | None = None,
    ) -> tuple[dict[str, Any], Any]:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            body["params"] = params
        if include_id:
            self._request_id += 1
            body["id"] = self._request_id

        response = self._post_with_retries(
            client,
            self.settings.datadog_mcp_server_url,
            self._build_mcp_headers(method, mcp_name=mcp_name),
            body,
            request_name="Datadog MCP request",
        )
        message = self._read_mcp_response(response)
        if "error" in message:
            raise RuntimeError(f"Datadog MCP request failed: {message['error']}")
        result = message.get("result") if isinstance(message, dict) else {}
        return (result if isinstance(result, dict) else {}, response)

    def _build_mcp_headers(self, method: str, *, mcp_name: str | None = None) -> dict[str, str]:
        headers = {
            "DD_API_KEY": self.settings.datadog_api_key,
            "DD_APPLICATION_KEY": self.settings.datadog_app_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": method,
            "MCP-Protocol-Version": self._mcp_protocol_version,
        }
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        if mcp_name:
            headers["Mcp-Name"] = mcp_name
        return headers

    def _read_mcp_response(self, response: Any) -> dict[str, Any]:
        text = getattr(response, "text", "") or ""
        if getattr(response, "status_code", 200) == 202 or not text.strip():
            return {}

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for message in self._read_sse_messages(text):
                if "error" in message or "result" in message:
                    return message
            return {}
        return response.json()

    def _read_sse_messages(self, text: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for raw_event in text.replace("\r\n", "\n").split("\n\n"):
            data_lines = []
            for line in raw_event.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").strip())
            if not data_lines:
                continue
            try:
                message = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages

    def _extract_logs_from_tool_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        structured_content = result.get("structuredContent")
        if structured_content is not None:
            candidates.append(structured_content)
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                candidates.append(self._json_from_text(str(item["text"])))
            elif item.get("type") == "json":
                candidates.append(item.get("json") or item.get("data"))

        logs: list[dict[str, Any]] = []
        for candidate in candidates:
            logs.extend(self._extract_logs_from_candidate(candidate))
        if not logs:
            logger.warning("Datadog MCP log search returned no parseable log payloads.")
        return logs

    def _extract_logs_from_candidate(self, candidate: Any) -> list[dict[str, Any]]:
        if candidate is None:
            return []
        if isinstance(candidate, list):
            return [self._normalize_log_payload(item) for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in ("data", "logs", "items", "events", "results"):
                value = candidate.get(key)
                if isinstance(value, list):
                    return [self._normalize_log_payload(item) for item in value if isinstance(item, dict)]
            return [self._normalize_log_payload(candidate)]
        return []

    def _normalize_log_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload.get("attributes"), dict):
            return payload
        attributes = {key: value for key, value in payload.items() if key not in {"id", "type"}}
        return {
            "id": str(payload.get("id", "")),
            "type": str(payload.get("type", "log")),
            "attributes": attributes,
        }

    def _json_from_text(self, text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, character in enumerate(cleaned):
                if character not in "{[":
                    continue
                try:
                    value, _end = decoder.raw_decode(cleaned[index:])
                    return value
                except json.JSONDecodeError:
                    continue
        return None

    def _format_mcp_time(self, value: datetime, value_type: Any) -> str | int:
        if value_type in {"integer", "number"}:
            return int(value.timestamp())
        return value.isoformat()

    def _initial_lookback_minutes(self) -> int:
        if self.settings.datadog_initial_lookback_days > 0:
            return self.settings.datadog_initial_lookback_days * 24 * 60
        return self.settings.datadog_lookback_minutes

    def _httpx_verify(self) -> HTTPX_VERIFY:
        if self.settings.datadog_mcp_ca_bundle:
            return self.settings.datadog_mcp_ca_bundle
        if not self.settings.datadog_mcp_verify_ssl:
            return False
        if self.settings.datadog_mcp_use_windows_cert_store and sys.platform == "win32":
            return self._windows_cert_store_context()
        return True

    def _windows_cert_store_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not hasattr(ssl, "enum_certificates"):
            return context

        pem_certificates: list[str] = []
        for store_name in ("ROOT", "CA"):
            try:
                certificates = ssl.enum_certificates(store_name)
            except OSError:
                continue
            for certificate, encoding, trust in certificates:
                if encoding != "x509_asn":
                    continue
                if trust is not True and "1.3.6.1.5.5.7.3.1" not in trust:
                    continue
                try:
                    pem_certificates.append(ssl.DER_cert_to_PEM_cert(certificate))
                except ValueError:
                    continue

        if pem_certificates:
            context.load_verify_locations(cadata="\n".join(pem_certificates))
        return context

    def _can_fallback_to_api(self, exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "mcp_read",
                "mcp_write",
                "returned no tools",
                "log search tool was not found",
            )
        )

    def _post_with_retries(
        self,
        client: Any,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        request_name: str,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response
            except Exception as exc:  # pragma: no cover - network behavior.
                last_error = exc
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                response_text = getattr(response, "text", "")
                if status_code is not None and 400 <= status_code < 500:
                    logger.warning(
                        f"{request_name} failed with a non-retryable response.",
                        extra={
                            "status_code": status_code,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "response_body": response_text[:1000],
                            "query": payload.get("filter", {}).get("query"),
                        },
                    )
                    raise
                sleep_seconds = 0.5 * (2**attempt)
                logger.warning(
                    f"{request_name} failed; retrying.",
                    extra={
                        "attempt": attempt + 1,
                        "sleep_seconds": sleep_seconds,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "response_body": response_text[:1000],
                        "query": payload.get("filter", {}).get("query"),
                    },
                )
                time.sleep(sleep_seconds)
        assert last_error is not None
        raise last_error
