"""Tests for the system events module and query helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmd_events import cmd_events, format_usage_for_telegram
from events import (
    normalize_telegram_callback,
    normalize_telegram_command,
    query_events,
    query_telegram_usage,
    record_event,
)
from store import open_db


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


class TestTelegramUsage:
    """Privacy-safe normalization and usage aggregation."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/clear", "clear"),
            ("/review current", "review"),
            ("/codex inspect private prompt text", "codex"),
            ("/llm-log@zdrowskit_bot 5", "llm_log"),
            ("not a command", None),
        ],
    )
    def test_normalize_command_discards_arguments(
        self, text: str, expected: str | None
    ) -> None:
        assert normalize_telegram_command(text) == expected

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ("ctx_accept:secret-token", "ctx_accept"),
            ("add_type:add-123:2", "add_type"),
            ("model_group:chat", "model_group"),
            ("agent:on:codex", "agent_on_codex"),
            ("", None),
        ],
    )
    def test_normalize_callback_discards_volatile_payloads(
        self, data: str, expected: str | None
    ) -> None:
        assert normalize_telegram_callback(data) == expected

    def test_query_telegram_usage_aggregates_counts_and_dates(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        first_id = record_event(
            in_memory_db,
            "telegram",
            "command",
            "Telegram command: /clear",
            {"action": "clear"},
        )
        second_id = record_event(
            in_memory_db,
            "telegram",
            "command",
            "Telegram command: /clear",
            {"action": "clear"},
        )
        callback_id = record_event(
            in_memory_db,
            "telegram",
            "callback",
            "Telegram callback: add_type",
            {"action": "add_type"},
        )
        assert first_id is not None
        assert second_id is not None
        assert callback_id is not None
        in_memory_db.execute(
            "UPDATE events SET ts = ? WHERE id = ?",
            ("2026-05-01T08:00:00+00:00", first_id),
        )
        in_memory_db.execute(
            "UPDATE events SET ts = ? WHERE id = ?",
            ("2026-05-03T09:30:00+00:00", second_id),
        )
        in_memory_db.execute(
            "UPDATE events SET ts = ? WHERE id = ?",
            ("2026-05-02T10:00:00+00:00", callback_id),
        )
        in_memory_db.commit()

        rows = query_telegram_usage(in_memory_db, since="2026-05-01T00:00:00+00:00")

        assert rows == [
            {
                "kind": "command",
                "action": "clear",
                "count": 2,
                "first_used": "2026-05-01T08:00:00+00:00",
                "last_used": "2026-05-03T09:30:00+00:00",
            },
            {
                "kind": "callback",
                "action": "add_type",
                "count": 1,
                "first_used": "2026-05-02T10:00:00+00:00",
                "last_used": "2026-05-02T10:00:00+00:00",
            },
        ]

    def test_format_usage_for_telegram_groups_commands_and_buttons(self) -> None:
        text = format_usage_for_telegram(
            [
                {
                    "kind": "command",
                    "action": "clear",
                    "count": 4,
                    "first_used": "2026-05-01T08:00:00+00:00",
                    "last_used": "2026-05-03T09:30:00+00:00",
                },
                {
                    "kind": "callback",
                    "action": "add_type",
                    "count": 2,
                    "first_used": "2026-05-02T10:00:00+00:00",
                    "last_used": "2026-05-02T10:00:00+00:00",
                },
            ]
        )

        assert "*Commands*" in text
        assert "`/clear` — 4 use(s)" in text
        assert "*Buttons*" in text
        assert "`add_type` — 2 use(s)" in text

    def test_events_usage_cli_outputs_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "usage.db"
        conn = open_db(db_path)
        record_event(
            conn,
            "telegram",
            "command",
            "Telegram command: /clear",
            {"action": "clear"},
        )
        conn.close()

        cmd_events(
            SimpleNamespace(
                db=str(db_path),
                usage=True,
                since=None,
                category=None,
                kind=None,
                limit=100,
                json=True,
            )
        )

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["kind"] == "command"
        assert rows[0]["action"] == "clear"
        assert rows[0]["count"] == 1
