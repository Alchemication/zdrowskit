"""Track the weekly check-in asked when a week has gone unusually quiet."""

from __future__ import annotations

import sqlite3

NAME = "quiet week check-in"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the one-row-per-week check-in ledger.

    The row exists to stop the question being asked twice. A check-in that
    repeated itself would be nagging, and nagging about not training is the
    fastest way to make someone turn a coach off.

    ``answered_at`` stays null when the person says nothing, which is a normal
    and permitted outcome — they may be in a meeting, or having the exact kind
    of week that made the question worth asking. Silence is counted, never
    chased.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS checkin (
            week_start   TEXT PRIMARY KEY,
            asked_at     TEXT NOT NULL,
            sessions     INTEGER,
            expected     REAL,
            message_id   INTEGER,
            llm_call_id  INTEGER,
            answered_at  TEXT,
            answer       TEXT,
            note         TEXT
        );
        """
    )
