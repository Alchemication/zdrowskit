"""Add per-kilometre heart rate to workout splits."""

from __future__ import annotations

import sqlite3

NAME = "workout split heart rate"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add heart-rate columns to workout_split.

    hr_coverage records the fraction of the split's elapsed time backed by
    heart-rate samples. It is populated even when hr_avg and hr_max are NULL, so
    a split with no usable heart rate is distinguishable from one that was never
    measured.
    """
    conn.executescript(
        """
        ALTER TABLE workout_split ADD COLUMN hr_avg REAL;
        ALTER TABLE workout_split ADD COLUMN hr_max INTEGER;
        ALTER TABLE workout_split ADD COLUMN hr_coverage REAL;
        """
    )
