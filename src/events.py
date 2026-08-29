"""System event log for daemon decisions and actions.

Writes coarse-grained diagnostic events (nudge fired/skipped, import done,
notify decided, context edited, etc.) to the ``events`` table so the user can
inspect how often things happen and how the system reacts.

Each event has:
    - category: coarse filter group (nudge, import, notify, chat, context,
      coach, insights, daemon, telegram)
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
    "ingest",
    "notify",
    "chat",
    "context",
    "coach",
    "insights",
    "daemon",
    "telegram",
)


def normalize_telegram_command(text: str) -> str | None:
    """Return a privacy-safe command name from a Telegram message.

    Arguments are intentionally discarded because they may contain health
    details, notification preferences, or coding-agent prompts.

    Args:
        text: Full Telegram message text beginning with ``/``.

    Returns:
        Canonical command name without the leading slash, or None when the
        input is not a command.
    """
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if not first.startswith("/"):
        return None
    command = first[1:].split("@", 1)[0].lower().replace("-", "_")
    return command or None


def normalize_telegram_callback(data: str) -> str | None:
    """Return a stable callback action without volatile callback payloads.

    Most callback data uses ``action:token:parameter``. Only the action is
    retained. Agent callbacks use ``agent:action:kind``; both action and kind
    are stable product choices, so they are retained while session data is not.

    Args:
        data: Raw Telegram callback data.

    Returns:
        Normalized action name, or None for empty/malformed data.
    """
    parts = [part.strip().lower() for part in data.split(":")]
    if not parts or not parts[0]:
        return None
    if parts[0] == "agent" and len(parts) >= 3:
        return f"agent_{parts[1]}_{parts[2]}"
    return parts[0]


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


def query_telegram_usage(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Aggregate privacy-safe Telegram command and callback usage.

    Args:
        conn: Open database connection.
        since: ISO timestamp (inclusive lower bound).
        until: ISO timestamp (exclusive upper bound).

    Returns:
        Rows with kind, action, count, first_used, and last_used, ordered by
        count then recency.
    """
    clauses = ["category = 'telegram'", "kind IN ('command', 'callback')"]
    params: list[str] = []
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if until:
        clauses.append("ts < ?")
        params.append(until)

    rows = conn.execute(
        "SELECT ts, kind, details_json FROM events "
        f"WHERE {' AND '.join(clauses)} ORDER BY ts, id",
        params,
    ).fetchall()
    aggregated: dict[tuple[str, str], dict] = {}
    for row in rows:
        try:
            details = json.loads(row[2]) if row[2] else {}
        except (TypeError, json.JSONDecodeError):
            continue
        action = details.get("action")
        if not isinstance(action, str) or not action:
            continue
        key = (row[1], action)
        entry = aggregated.setdefault(
            key,
            {
                "kind": row[1],
                "action": action,
                "count": 0,
                "first_used": row[0],
                "last_used": row[0],
            },
        )
        entry["count"] += 1
        entry["last_used"] = row[0]

    return sorted(
        aggregated.values(),
        key=lambda entry: (entry["count"], entry["last_used"]),
        reverse=True,
    )
