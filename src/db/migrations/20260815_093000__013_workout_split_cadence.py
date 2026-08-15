"""Add per-kilometre step cadence to workout splits."""

from __future__ import annotations

import sqlite3

NAME = "workout split cadence"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add cadence columns to workout_split.

    Stride length is deliberately not stored: it is exactly
    1000 / (cadence_spm * pace_min_km), so a column would be a second copy of
    two values already present and could drift from them.
    """
    conn.executescript(
        """
        ALTER TABLE workout_split ADD COLUMN cadence_spm REAL;
        ALTER TABLE workout_split ADD COLUMN cadence_coverage REAL;
        """
    )
