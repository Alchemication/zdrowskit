"""Chat tool definitions and execution for interactive data queries.

Provides a ``run_sql`` tool that lets the LLM execute read-only SQL
against the health database, plus a dispatcher that routes tool calls.

Public API:
    run_sql_tool         — tool definition schema for litellm
    all_chat_tools       — combined tool list (run_sql + update_context)
    execute_run_sql      — execute a read-only SQL query, return JSON rows
    execute_tool         — dispatch a tool call by name

Example:
    tools = all_chat_tools()
    result = execute_tool("run_sql", {"query": "SELECT ...", "limit": 50}, db_path)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum rows returned by a single query.
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

# Timeout in seconds for SQL execution.
_SQL_TIMEOUT = 5


def run_sql_tool() -> list[dict]:
    """Tool definition for read-only SQL queries against the health database.

    Returns:
        A list with a single tool definition dict for litellm.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "run_sql",
                "description": (
                    "Execute a read-only SQL query against the health database "
                    "and return results as JSON. Key tables: 'daily' (one row "
                    "per day with health metrics), 'workout_all' (all workouts "
                    "including manual entries), 'sleep_all' (all sleep data "
                    "including manual entries). Both '_all' views have a "
                    "'source' column. Prefer 'workout_all' for workout/session "
                    "questions (runs, pace, distance, elevation, workout HR, "
                    "session trends) and 'daily' for day-level health metrics "
                    "(HRV, resting HR, steps, recovery, mobility). See the "
                    "schema reference in your system prompt for column details "
                    "and units."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "A SELECT SQL query.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Max rows to return. Default {_DEFAULT_LIMIT}, "
                                f"max {_MAX_LIMIT}."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        }
    ]


def all_chat_tools() -> list[dict]:
    """Return the combined tool list for chat: run_sql + update_context.

    Returns:
        A list of tool definition dicts for litellm.
    """
    from llm_context import context_update_tool

    return run_sql_tool() + context_update_tool()


def execute_run_sql(db_path: Path, arguments: dict) -> str:
    """Execute a read-only SQL query and return JSON rows.

    Opens a separate read-only SQLite connection, validates that the
    statement is a SELECT, wraps it with a LIMIT clause, and executes
    with a timeout.

    Args:
        db_path: Path to the SQLite database file.
        arguments: Parsed tool call arguments with ``query`` and optional ``limit``.

    Returns:
        A JSON string: either a list of row dicts on success,
        or an ``{"error": "..."}`` object on failure.
    """
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "Empty query."})

    # Only allow SELECT statements.
    first_word = query.lstrip("( \t\n").split()[0].upper() if query.strip() else ""
    if first_word != "SELECT":
        return json.dumps({"error": "Only SELECT queries are allowed."})

    limit = min(int(arguments.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT)

    # Wrap with LIMIT to cap result size.
    wrapped = f"SELECT * FROM ({query}) LIMIT {limit}"

    result_holder: list[str] = [json.dumps({"error": "Query timed out."})]

    def _run() -> None:
        try:
            uri = f"file:{db_path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=_SQL_TIMEOUT)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(wrapped)
                rows = [dict(row) for row in cursor.fetchall()]
                result_holder[0] = json.dumps(rows, default=str)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("run_sql failed: %s", exc)
            result_holder[0] = json.dumps({"error": str(exc)})

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_SQL_TIMEOUT + 1)

    return result_holder[0]


def execute_tool(name: str, arguments: dict, db_path: Path) -> str:
    """Dispatch a tool call by name.

    Args:
        name: The tool function name (e.g. ``"run_sql"``).
        arguments: Parsed JSON arguments for the tool.
        db_path: Path to the SQLite database file.

    Returns:
        A JSON string with the tool result.
    """
    if name == "run_sql":
        return execute_run_sql(db_path, arguments)
    return json.dumps({"error": f"Unknown tool: {name}"})
