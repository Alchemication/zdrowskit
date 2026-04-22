"""Add subjective feel tracking to manual activity tables."""

from __future__ import annotations

import sqlite3

NAME = "feel columns on manual_workout and manual_sleep"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add ``feel`` and ``feel_adjusted`` columns to both manual tables.

    ``feel`` stores the user's subjective read (e.g. ``easy``, ``hard``,
    ``wrecked``). ``feel_adjusted`` is 1 when deterministic multipliers were
    applied on top of the clone, letting downstream reports discount
    synthesised numbers vs. raw measurements.
    """
    conn.executescript(
        """
        ALTER TABLE manual_workout ADD COLUMN feel TEXT;
        ALTER TABLE manual_workout ADD COLUMN feel_adjusted INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE manual_sleep ADD COLUMN feel TEXT;
        ALTER TABLE manual_sleep ADD COLUMN feel_adjusted INTEGER NOT NULL DEFAULT 0;
        """
    )
