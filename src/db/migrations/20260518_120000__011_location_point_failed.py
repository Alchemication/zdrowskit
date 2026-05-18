"""Persist reverse-geocode failures so backfills survive restarts."""

from __future__ import annotations

import sqlite3

NAME = "location point failed"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add the failed-lookup cache table."""
    conn.executescript(
        """
        CREATE TABLE location_point_failed (
            lat_round       REAL NOT NULL,
            lon_round       REAL NOT NULL,
            last_attempt_at TEXT NOT NULL,
            attempt_count   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (lat_round, lon_round)
        );
        """
    )
