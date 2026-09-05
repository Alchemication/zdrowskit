"""Move recorded HIIT workouts out of the uncategorised bucket."""

from __future__ import annotations

import sqlite3

NAME = "hiit category"

# Matches the names ``parsers.workouts._CATEGORY_MAP`` now maps to ``hiit``.
_HIIT_TYPES = ("high intensity interval training", "hiit")


def upgrade(conn: sqlite3.Connection) -> None:
    """Recategorise historical HIIT rows from ``other`` to ``hiit``.

    The parser categorises new imports, but rows already stored keep whatever
    category they were written with. Without this backfill a profile's HIIT
    history would sit in two buckets at once, and a weekly target counting HIIT
    sessions would silently miss everything imported before today.

    Only rows currently in ``other`` are touched, so a category assigned
    deliberately elsewhere is never overwritten.
    """
    placeholders = ", ".join("?" * len(_HIIT_TYPES))
    for table in ("workout", "manual_workout"):
        conn.execute(
            f"UPDATE {table} SET category = 'hiit' "
            f"WHERE category = 'other' AND lower(type) IN ({placeholders})",
            _HIIT_TYPES,
        )
