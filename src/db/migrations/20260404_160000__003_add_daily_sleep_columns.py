"""Add sleep columns to daily."""

from __future__ import annotations

import sqlite3

NAME = "add daily sleep columns"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add sleep tracking columns to daily."""
    for column in (
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
    ):
        conn.execute(f"ALTER TABLE daily ADD COLUMN {column} REAL")
