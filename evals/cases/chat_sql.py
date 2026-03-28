"""Eval: chat SQL tool call validity.

Mirrors ``_chat_reply()`` in ``src/daemon.py`` — but only the first
LLM call (no tool-calling loop).

Scenarios:
    baseline — Fresh chat prompt with an explicit data question.
               Model should produce a run_sql tool call with valid SQL.

Assertions:
    - Response includes a run_sql tool call
    - SQL is a valid SELECT statement
    - SQL references only known table/column names from the schema
"""

from __future__ import annotations

import json

from evals.data.scenarios import baseline
from evals.framework import (
    AssertionResult,
    Eval,
    sql_is_valid_select,
    sql_uses_valid_columns,
)

_CHAT_QUESTION = "What's my average resting heart rate by week for the last month?"


def _extract_sql_from_tool_calls(tool_calls: list | None) -> list[str]:
    """Pull SQL queries from run_sql tool calls."""
    queries: list[str] = []
    if not tool_calls:
        return queries
    for tc in tool_calls:
        fn = getattr(tc, "function", None) or tc.get("function", {})
        name = getattr(fn, "name", None) or fn.get("name", "")
        if name != "run_sql":
            continue
        args_str = getattr(fn, "arguments", None) or fn.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            continue
        query = args.get("query", "")
        if query:
            queries.append(query)
    return queries


class ChatSqlEval(Eval):
    name = "chat_sql"

    def eval_scenarios(self) -> list[tuple[str, callable, dict]]:
        from tools import all_chat_tools

        return [
            (
                "baseline",
                baseline,
                {
                    "prompt_file": "chat_prompt",
                    "max_tokens": 1024,
                    "tools": all_chat_tools(),
                    "extra_messages": [
                        {"role": "user", "content": _CHAT_QUESTION},
                    ],
                },
            ),
        ]

    def assertions(
        self,
        response: str,
        tool_calls: list | None = None,
        health_data: dict | None = None,
    ) -> list[AssertionResult]:
        queries = _extract_sql_from_tool_calls(tool_calls)

        if not queries:
            return [
                AssertionResult(
                    name="has_sql",
                    passed=False,
                    detail="No run_sql tool call found",
                )
            ]

        results: list[AssertionResult] = [
            AssertionResult(
                name="has_sql",
                passed=True,
                detail=f"{len(queries)} query/queries",
            )
        ]

        for i, query in enumerate(queries):
            prefix = f"q{i}" if len(queries) > 1 else "q"

            select_check = sql_is_valid_select(query)
            select_check.name = f"{prefix}_select"
            results.append(select_check)

            col_check = sql_uses_valid_columns(query)
            col_check.name = f"{prefix}_columns"
            results.append(col_check)

        return results
