"""SQLite-backed cache for eval executions.

Stores successful eval executions keyed by a strong JSON payload plus its
SHA-256 hash. This keeps repeated local eval runs fast without coupling eval
state to the product's normal LLM call logs.

Note: The cache key includes the rendered messages and tool schemas, so
prompt template edits automatically invalidate cached entries. However,
changes to scenario function *code* (evals/data/scenarios.py) are not
captured — use ``--no-cache`` after editing scenarios.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_CACHE_DB = Path(__file__).resolve().parent / ".cache.sqlite"

_DDL = """
CREATE TABLE IF NOT EXISTS eval_cache (
    key_hash        TEXT PRIMARY KEY,
    key_json        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    suite           TEXT NOT NULL,
    category        TEXT NOT NULL,
    model           TEXT NOT NULL,
    response_text   TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL,
    query_rows_json TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    latency_s       REAL NOT NULL,
    cost            REAL
);

CREATE INDEX IF NOT EXISTS eval_cache_case_model
ON eval_cache(case_id, model);
"""


@dataclass
class EvalCacheEntry:
    """Cached eval execution payload."""

    key_hash: str
    key_json: str
    response_text: str
    tool_calls: list[dict]
    query_rows: list[dict]
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost: float | None


def cache_db_path() -> Path:
    """Return the eval cache database path.

    Returns:
        Filesystem path for the shared eval cache database.
    """
    return _CACHE_DB


def build_cache_key(payload: dict) -> tuple[str, str]:
    """Build a stable cache key from a JSON-serialisable payload.

    Args:
        payload: Strong cache-key payload with all request-shaping inputs.

    Returns:
        A ``(key_hash, key_json)`` tuple.
    """
    key_json = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    key_hash = hashlib.sha256(key_json.encode("utf-8")).hexdigest()
    return key_hash, key_json


def load_cache_entry(key_hash: str) -> EvalCacheEntry | None:
    """Load a cached execution by key hash.

    Args:
        key_hash: SHA-256 cache key hash.

    Returns:
        Cached entry if present, otherwise None.
    """
    conn = _open_cache_db()
    try:
        row = conn.execute(
            """
            SELECT key_hash, key_json, response_text, tool_calls_json,
                   query_rows_json, input_tokens, output_tokens, latency_s, cost
            FROM eval_cache
            WHERE key_hash = ?
            """,
            (key_hash,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return EvalCacheEntry(
        key_hash=row["key_hash"],
        key_json=row["key_json"],
        response_text=row["response_text"],
        tool_calls=json.loads(row["tool_calls_json"]),
        query_rows=json.loads(row["query_rows_json"]),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        latency_s=row["latency_s"],
        cost=row["cost"],
    )


def save_cache_entry(
    *,
    key_hash: str,
    key_json: str,
    case_id: str,
    suite: str,
    category: str,
    model: str,
    response_text: str,
    tool_calls: list[dict],
    query_rows: list[dict],
    input_tokens: int,
    output_tokens: int,
    latency_s: float,
    cost: float | None,
) -> None:
    """Persist a cached eval execution.

    Args:
        key_hash: SHA-256 cache key hash.
        key_json: Full strong cache-key payload as JSON.
        case_id: Eval case id.
        suite: Eval suite name.
        category: Eval category name.
        model: Model string.
        response_text: Final model response text.
        tool_calls: Serialised tool calls.
        query_rows: Query rows accumulated during the tool loop.
        input_tokens: Total input tokens used.
        output_tokens: Total output tokens used.
        latency_s: Measured execution latency.
        cost: Total completion cost, if available.
    """
    conn = _open_cache_db()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO eval_cache (
                key_hash, key_json, created_at, case_id, suite, category, model,
                response_text, tool_calls_json, query_rows_json,
                input_tokens, output_tokens, latency_s, cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_hash,
                key_json,
                datetime.now(timezone.utc).isoformat(),
                case_id,
                suite,
                category,
                model,
                response_text,
                json.dumps(tool_calls, sort_keys=True),
                json.dumps(query_rows, sort_keys=True),
                input_tokens,
                output_tokens,
                latency_s,
                cost,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _open_cache_db() -> sqlite3.Connection:
    """Open the eval cache database and ensure schema exists.

    Returns:
        Open sqlite3 connection with row access by column name.
    """
    conn = sqlite3.connect(str(_CACHE_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn
