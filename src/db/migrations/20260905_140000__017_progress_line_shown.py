"""Remember the last progress line a nudge actually showed."""

from __future__ import annotations

import sqlite3

NAME = "progress line shown"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the single-row store for the last shown progress fingerprint.

    Nudges fire up to twice a day and the weekly bars move three or four times
    a week, so repeating the same line on every message teaches the reader to
    skip the top of it — which is where the nudge itself starts. Suppressing an
    unchanged line needs exactly one fact: what was last said.

    One row per profile database, pinned by a CHECK so it cannot accumulate.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS progress_line_shown (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            fingerprint  TEXT NOT NULL,
            line         TEXT NOT NULL,
            shown_at     TEXT NOT NULL
        );
        """
    )
