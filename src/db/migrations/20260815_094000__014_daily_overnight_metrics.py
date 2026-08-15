"""Add overnight respiratory rate and sleeping wrist temperature to daily."""

from __future__ import annotations

import sqlite3

NAME = "daily overnight metrics"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add the two overnight recovery columns.

    Both are stored under the night-start date, matching the sleep columns, so a
    night's sleep, respiratory rate, and wrist temperature share one row.
    """
    conn.executescript(
        """
        ALTER TABLE daily ADD COLUMN respiratory_rate REAL;
        ALTER TABLE daily ADD COLUMN sleeping_wrist_temp_c REAL;
        """
    )
