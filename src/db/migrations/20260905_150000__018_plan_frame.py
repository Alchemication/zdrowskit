"""Cache whether the training plan is currently the right frame to judge by."""

from __future__ import annotations

import sqlite3

NAME = "plan frame"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the single-row cache for the plan-frame decision.

    The decision is about the person's life, which changes over days, not
    between two notifications an hour apart. Caching it keeps one answer
    serving every surface for as long as it holds, so the progress strip cannot
    appear on one nudge and vanish from the next for no reason the reader can
    see.

    ``reason`` and ``llm_call_id`` are stored because a suppressed strip is
    invisible by construction: without them, a wrong decision looks exactly
    like a working feature that had nothing to say.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plan_frame (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            mode         TEXT NOT NULL,
            reason       TEXT,
            context_hash TEXT NOT NULL,
            llm_call_id  INTEGER,
            decided_at   TEXT NOT NULL
        );
        """
    )
