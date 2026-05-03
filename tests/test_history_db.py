from pathlib import Path

from logscraper.models import ErrorStatus, LogEvent
from logscraper.store.history_db import HistoryDB


def test_history_tracks_occurrences_and_status(tmp_path: Path):
    db = HistoryDB(tmp_path / "history.db")
    event = LogEvent(
        fingerprint="abc123",
        exception_type="System.InvalidOperationException",
        error_message="bad state",
        service="orders",
        environment="prod",
    )

    record = db.upsert_occurrence(event)
    assert record.occurrence_count == 1
    assert record.status == ErrorStatus.OPEN

    updated = db.upsert_occurrence(event)
    assert updated.occurrence_count == 2

    planned = db.update_status(event.fingerprint, ErrorStatus.FIX_PLANNED)
    assert planned.status == ErrorStatus.FIX_PLANNED

    reviewed = db.attach_pr(event.fingerprint, "https://github.com/acme/orders/pull/1")
    assert reviewed.pr_url == "https://github.com/acme/orders/pull/1"
    assert reviewed.status == ErrorStatus.IN_REVIEW
