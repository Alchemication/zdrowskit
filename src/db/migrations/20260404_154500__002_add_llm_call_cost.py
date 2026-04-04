"""Add cost tracking to llm_call."""

from __future__ import annotations

import sqlite3

NAME = "add llm_call cost"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add the cost column to llm_call."""
    conn.execute("ALTER TABLE llm_call ADD COLUMN cost REAL")
