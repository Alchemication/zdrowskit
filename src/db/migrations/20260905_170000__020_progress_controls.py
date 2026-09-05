"""Cache empty target decisions and persist progress controls and note replies."""

from __future__ import annotations

import sqlite3

NAME = "progress controls and check-in replies"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add durable state for successful extractions and user interactions."""
    conn.executescript("""
        CREATE TABLE target_derivation (
            week_start TEXT PRIMARY KEY,
            strategy_hash TEXT NOT NULL
        );
        CREATE TABLE progress_preference (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            paused INTEGER NOT NULL DEFAULT 0
        );
        ALTER TABLE checkin ADD COLUMN note_prompt_id INTEGER;
        CREATE UNIQUE INDEX checkin_note_prompt ON checkin(note_prompt_id);
    """)
