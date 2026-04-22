"""Add per-kilometre workout splits for route-based runs."""

from __future__ import annotations

import sqlite3

NAME = "workout split table"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the workout_split table and supporting index."""
    conn.executescript(
        """
        CREATE TABLE workout_split (
            start_utc         TEXT NOT NULL REFERENCES workout(start_utc) ON DELETE CASCADE,
            km_index          INTEGER NOT NULL,
            pace_min_km       REAL NOT NULL,
            avg_speed_ms      REAL,
            elevation_gain_m  REAL,
            elevation_loss_m  REAL,
            PRIMARY KEY (start_utc, km_index)
        );

        CREATE INDEX workout_split_pace ON workout_split(pace_min_km);
        """
    )
