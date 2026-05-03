from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from logscraper.models import ErrorRecord, ErrorStatus, LogEvent


class HistoryDB:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS errors (
                    fingerprint TEXT PRIMARY KEY,
                    exception_type TEXT,
                    message TEXT NOT NULL,
                    service TEXT,
                    environment TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    pr_url TEXT,
                    raw_payload_json TEXT
                )
                """
            )

    def get_record(self, fingerprint: str) -> ErrorRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM errors WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def upsert_occurrence(self, event: LogEvent, *, status: ErrorStatus = ErrorStatus.OPEN) -> ErrorRecord:
        existing = self.get_record(event.fingerprint)
        if existing:
            return self.increment_occurrence(event.fingerprint, event.timestamp)

        payload_json = json.dumps(event.raw_payload, default=str, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO errors (
                    fingerprint, exception_type, message, service, environment,
                    first_seen, last_seen, occurrence_count, status, pr_url, raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.fingerprint,
                    event.exception_type,
                    event.error_message,
                    event.service,
                    event.environment,
                    event.timestamp.isoformat(),
                    event.timestamp.isoformat(),
                    1,
                    _status_value(status),
                    None,
                    payload_json,
                ),
            )
        record = self.get_record(event.fingerprint)
        assert record is not None
        return record

    def increment_occurrence(self, fingerprint: str, seen_at: datetime | None = None) -> ErrorRecord:
        seen = seen_at or datetime.now().astimezone()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE errors
                SET occurrence_count = occurrence_count + 1,
                    last_seen = ?
                WHERE fingerprint = ?
                """,
                (seen.isoformat(), fingerprint),
            )
        record = self.get_record(fingerprint)
        if record is None:
            raise KeyError(f"Unknown fingerprint: {fingerprint}")
        return record

    def update_status(self, fingerprint: str, status: ErrorStatus) -> ErrorRecord:
        with self._connect() as connection:
            connection.execute(
                "UPDATE errors SET status = ? WHERE fingerprint = ?",
                (_status_value(status), fingerprint),
            )
        record = self.get_record(fingerprint)
        if record is None:
            raise KeyError(f"Unknown fingerprint: {fingerprint}")
        return record

    def attach_pr(self, fingerprint: str, pr_url: str | None, *, status: ErrorStatus = ErrorStatus.IN_REVIEW) -> ErrorRecord:
        with self._connect() as connection:
            connection.execute(
                "UPDATE errors SET pr_url = ?, status = ? WHERE fingerprint = ?",
                (pr_url, _status_value(status), fingerprint),
            )
        record = self.get_record(fingerprint)
        if record is None:
            raise KeyError(f"Unknown fingerprint: {fingerprint}")
        return record

    def list_records(self) -> list[ErrorRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM errors ORDER BY last_seen DESC").fetchall()
        return [self._row_to_record(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> ErrorRecord:
        return ErrorRecord(
            fingerprint=row["fingerprint"],
            exception_type=row["exception_type"],
            message=row["message"],
            service=row["service"],
            environment=row["environment"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
            occurrence_count=int(row["occurrence_count"]),
            status=ErrorStatus(row["status"]),
            pr_url=row["pr_url"],
        )


def _status_value(status: ErrorStatus | str) -> str:
    return status.value if isinstance(status, ErrorStatus) else str(status)
