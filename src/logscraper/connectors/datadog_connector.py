from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from logscraper.config import AppSettings


logger = logging.getLogger(__name__)


class DatadogConnector:
    def __init__(self, settings: AppSettings, *, timeout_seconds: float = 20.0) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def build_query(self) -> str:
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
        now: datetime | None = None,
    ) -> dict[str, Any]:
        to_time = now or datetime.now(timezone.utc)
        from_time = to_time - timedelta(minutes=self.settings.datadog_lookback_minutes)
        payload: dict[str, Any] = {
            "filter": {
                "query": self.build_query(),
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

    def fetch_logs(self) -> list[dict[str, Any]]:
        if not self.settings.datadog_api_key or not self.settings.datadog_app_key:
            raise ValueError("Datadog API and app keys are required. Use DATADOG_API_KEY/DATADOG_APP_KEY or DD_API_KEY/DD_APP_KEY.")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - requires optional deps absent.
            raise RuntimeError("httpx is required for Datadog API calls. Install project dependencies.") from exc

        logs: list[dict[str, Any]] = []
        cursor: str | None = None
        url = f"{self.settings.datadog_base_url}/api/v2/logs/events/search"
        headers = {
            "DD-API-KEY": self.settings.datadog_api_key,
            "DD-APPLICATION-KEY": self.settings.datadog_app_key,
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout_seconds) as client:
            while len(logs) < self.settings.max_logs_per_run:
                payload = self.build_search_payload(cursor=cursor)
                response = self._post_with_retries(client, url, headers, payload)
                body = response.json()
                logs.extend(body.get("data", []))
                cursor = (
                    body.get("meta", {})
                    .get("page", {})
                    .get("after")
                )
                if not cursor:
                    break
        return logs[: self.settings.max_logs_per_run]

    def _post_with_retries(self, client: Any, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response
            except Exception as exc:  # pragma: no cover - network behavior.
                last_error = exc
                sleep_seconds = 0.5 * (2**attempt)
                logger.warning(
                    "Datadog request failed; retrying.",
                    extra={"attempt": attempt + 1, "sleep_seconds": sleep_seconds},
                )
                time.sleep(sleep_seconds)
        assert last_error is not None
        raise last_error
