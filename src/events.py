"""System event log for daemon decisions and actions.

Writes coarse-grained diagnostic events (nudge fired/skipped, import done,
notify decided, context edited, etc.) to the ``events`` table so the user can
inspect how often things happen and how the system reacts.

Each event has:
    - category: coarse filter group (nudge, import, notify, chat, context,
      coach, insights, daemon)
    - kind: fine-grained action within the category (fired, llm_skip,
      rate_limited, quiet_deferred, ...)
    - summary: one-line human-readable description
    - details_json: optional structured payload
    - llm_call_id: FK into llm_call for kinds that touched the LLM
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CATEGORIES = (
    "nudge",
    "import",
    "notify",
    "chat",
    "context",
    "coach",
    "insights",
    "daemon",
)


def record_event(
    conn: sqlite3.Connection,
    category: str,
    kind: str,
    summary: str,
    details: dict | None = None,
    llm_call_id: int | None = None,
) -> int | None:
    """Insert an event row. Never raises — diagnostic-only.

    Args:
        conn: Open database connection.
        category: Coarse filter group (see CATEGORIES).
        kind: Fine-grained action name within the category.
        summary: One-line human-readable description.
        details: Optional structured payload (JSON-serialisable).
        llm_call_id: FK into llm_call when this event represents an LLM call.

    Returns:
        The inserted row id, or None if the write failed.
    """
    ts = datetime.now(timezone.utc).isoformat()
    try:
        cursor = conn.execute(
            """
            INSERT INTO events (ts, category, kind, summary, details_json, llm_call_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                category,
                kind,
                summary,
                json.dumps(details) if details else None,
                llm_call_id,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.Error:
        logger.warning("Failed to record event %s.%s", category, kind, exc_info=True)
        return None


def query_events(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return events matching the given filters, most recent first.

    Args:
        conn: Open database connection.
        category: Filter to a single category.
        kind: Filter to a single kind (usually combined with category).
        since: ISO timestamp (inclusive lower bound).
        until: ISO timestamp (exclusive upper bound).
        limit: Maximum rows to return.

    Returns:
        List of dicts with keys id, ts, category, kind, summary, details,
        llm_call_id.
    """
    clauses: list[str] = []
    params: list = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if until:
        clauses.append("ts < ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT id, ts, category, kind, summary, details_json, llm_call_id "
        f"FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "category": r[2],
            "kind": r[3],
            "summary": r[4],
            "details": json.loads(r[5]) if r[5] else None,
            "llm_call_id": r[6],
        }
        for r in rows
    ]
