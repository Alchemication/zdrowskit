"""System event log for daemon decisions and actions."""

from __future__ import annotations

import sqlite3

NAME = "events table"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the events table for system diagnostics."""
    conn.executescript(
        """
        CREATE TABLE events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            category      TEXT NOT NULL,
            kind          TEXT NOT NULL,
            summary       TEXT NOT NULL,
            details_json  TEXT,
            llm_call_id   INTEGER REFERENCES llm_call(id)
        );

        CREATE INDEX events_ts ON events(ts);
        CREATE INDEX events_category ON events(category);
        CREATE INDEX events_kind ON events(category, kind);
        """
    )
