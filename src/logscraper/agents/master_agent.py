from __future__ import annotations

from logscraper.models import ErrorStatus, LogEvent, ResolutionTask
from logscraper.store.history_db import HistoryDB


class MasterAgent:
    def __init__(self, history: HistoryDB) -> None:
        self.history = history

    def route_event(self, event: LogEvent) -> tuple[str, ResolutionTask | None]:
        existing = self.history.get_record(event.fingerprint)
        if existing and existing.status in {ErrorStatus.RESOLVED, ErrorStatus.IGNORED}:
            self.history.increment_occurrence(event.fingerprint, event.timestamp)
            return existing.status.value, None

        if existing:
            updated = self.history.increment_occurrence(event.fingerprint, event.timestamp)
            return "duplicate", None

        record = self.history.upsert_occurrence(event, status=ErrorStatus.OPEN)
        task = ResolutionTask(
            fingerprint=event.fingerprint,
            log_event=event,
            status=ErrorStatus.OPEN,
            occurrence_count=record.occurrence_count,
        )
        return "actionable", task
