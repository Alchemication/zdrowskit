"""Tests for the system events module and query helpers."""

from __future__ import annotations

import json
import sqlite3

import pytest

from events import query_events, record_event


class TestRecordEvent:
    """Round-trip writes into the events table."""

    def test_insert_and_read_back(self, in_memory_db: sqlite3.Connection) -> None:
        event_id = record_event(
            in_memory_db,
            "nudge",
            "fired",
            "Nudge sent (new_data)",
            details={"trigger": "new_data", "chars": 120},
            llm_call_id=None,
        )
        assert event_id is not None

        row = in_memory_db.execute(
            "SELECT category, kind, summary, details_json FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert row["category"] == "nudge"
        assert row["kind"] == "fired"
        assert row["summary"] == "Nudge sent (new_data)"
        assert json.loads(row["details_json"])["trigger"] == "new_data"

    def test_none_details_stored_as_null(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        record_event(in_memory_db, "daemon", "start", "Daemon started")
        row = in_memory_db.execute(
            "SELECT details_json FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["details_json"] is None

    def test_swallows_errors_returns_none(self) -> None:
        """A failing write should not raise — it's diagnostic-only."""
        conn = sqlite3.connect(":memory:")
        # No migrations → no events table; record_event must not raise.
        result = record_event(conn, "nudge", "fired", "x")
        assert result is None


class TestQueryEvents:
    """Filters and ordering on query_events."""

    @pytest.fixture
    def populated(self, in_memory_db: sqlite3.Connection) -> sqlite3.Connection:
        record_event(in_memory_db, "nudge", "fired", "first")
        record_event(in_memory_db, "nudge", "llm_skip", "second")
        record_event(in_memory_db, "import", "new_data", "third")
        record_event(in_memory_db, "coach", "fired", "fourth")
        return in_memory_db

    def test_returns_most_recent_first(self, populated: sqlite3.Connection) -> None:
        rows = query_events(populated)
        assert [r["summary"] for r in rows] == ["fourth", "third", "second", "first"]

    def test_filter_by_category(self, populated: sqlite3.Connection) -> None:
        rows = query_events(populated, category="nudge")
        assert len(rows) == 2
        assert {r["kind"] for r in rows} == {"fired", "llm_skip"}

    def test_filter_by_category_and_kind(self, populated: sqlite3.Connection) -> None:
        rows = query_events(populated, category="nudge", kind="fired")
        assert len(rows) == 1
        assert rows[0]["summary"] == "first"

    def test_limit_respected(self, populated: sqlite3.Connection) -> None:
        rows = query_events(populated, limit=2)
        assert len(rows) == 2
