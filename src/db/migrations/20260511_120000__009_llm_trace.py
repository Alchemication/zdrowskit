"""Add first-class trace grouping for related LLM calls."""

from __future__ import annotations

import sqlite3

NAME = "add llm trace grouping"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create llm_trace and link llm_call rows to it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_trace (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            surface         TEXT NOT NULL,
            metadata_json   TEXT
        )
    """)
    conn.execute(
        "ALTER TABLE llm_call ADD COLUMN trace_id INTEGER REFERENCES llm_trace(id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS llm_call_trace ON llm_call(trace_id)")
