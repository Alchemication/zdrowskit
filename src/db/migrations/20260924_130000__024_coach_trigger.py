"""Record when a goal-check trigger last forced a coach review."""

from __future__ import annotations

import sqlite3

NAME = "coach trigger ledger"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the ledger the coach's goal check uses for cooldowns.

    A trigger — a target missed most recent weeks, or the goals' review date
    passing — forces the weekly coach to propose something instead of skipping.
    Without a record of when each one last fired, a persistently missed target
    would be raised every Monday until the person gave in.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coach_trigger (
            key            TEXT PRIMARY KEY,
            first_raised   TEXT NOT NULL,
            last_raised    TEXT NOT NULL,
            llm_call_id    INTEGER
        );
        """
    )
