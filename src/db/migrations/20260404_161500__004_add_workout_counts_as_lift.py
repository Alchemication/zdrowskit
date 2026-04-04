"""Add workout counts_as_lift with backfill."""

from __future__ import annotations

import sqlite3

NAME = "add workout counts_as_lift"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add counts_as_lift and backfill historical workouts."""
    conn.execute(
        "ALTER TABLE workout ADD COLUMN counts_as_lift INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute(
        """
        UPDATE workout
        SET counts_as_lift = CASE
            WHEN lower(type) = 'traditional strength training' THEN 1
            WHEN lower(type) = 'functional strength training' AND duration_min >= 15 THEN 1
            ELSE 0
        END
        """
    )
