"""Eval framework — blueprint loading, case runner, assertion helpers, results.

The eval system is driven by ``evals/data/cases.json``, a human-labelled
dataset of test cases.  Each case specifies a scenario, config, and
expectations (must_contain / must_not_contain patterns, expected actions,
or SQL answer validation).

Building blocks:
    load_blueprints()     — read pinned context + health data
    build_eval_messages() — thin wrapper around build_messages() with pinned date
    run_case()            — load → scenario → LLM call → assertions
    print_results()       — rich table output

Assertion helpers:
    response_is_skip      — response is exactly SKIP
    response_is_not_skip  — response is not SKIP
    text_absent           — none of the patterns match
    text_present          — at least one pattern matches
    sql_is_valid_select   — SQL starts with SELECT
    sql_uses_valid_columns — SQL uses known schema identifiers
    build_eval_db         — in-memory SQLite from health_data.json
    execute_sql           — run SQL against eval DB
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# Ensure src/ is importable.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_BLUEPRINTS_DIR = Path(__file__).resolve().parent / "data" / "blueprints"
_CASES_PATH = Path(__file__).resolve().parent / "data" / "cases.json"

DEFAULT_MODEL = "anthropic/claude-opus-4-6"


# ---------------------------------------------------------------------------
# Blueprint loading
# ---------------------------------------------------------------------------

_CONTEXT_STEMS = ["me", "goals", "plan", "log", "history", "baselines"]


def load_blueprints(
    prompt_file: str = "prompt",
) -> tuple[dict[str, str], dict, dict]:
    """Load pinned context, health data, and metadata from committed blueprints.

    Prompt templates are loaded live from ``src/prompts/`` (version-controlled
    code).  User context files come from the blueprint snapshot.

    Returns:
        (context, health_data, metadata) tuple.
    """
    from config import PROMPTS_DIR

    context: dict[str, str] = {}

    prompt_path = PROMPTS_DIR / f"{prompt_file}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt template missing: {prompt_path}")
    context["prompt"] = prompt_path.read_text(encoding="utf-8")

    soul_path = PROMPTS_DIR / "soul.md"
    if soul_path.exists():
        context["soul"] = soul_path.read_text(encoding="utf-8")

    ctx_dir = _BLUEPRINTS_DIR / "context"
    for stem in _CONTEXT_STEMS:
        path = ctx_dir / f"{stem}.md"
        context[stem] = (
            path.read_text(encoding="utf-8") if path.exists() else "(not provided)"
        )

    hd_path = _BLUEPRINTS_DIR / "health_data.json"
    health_data = json.loads(hd_path.read_text(encoding="utf-8"))

    meta_path = _BLUEPRINTS_DIR / "metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))

    return context, health_data, metadata


def build_eval_messages(
    context: dict[str, str],
    health_data: dict,
    metadata: dict,
    baselines: str | None = None,
    week_complete: bool | None = None,
) -> list[dict[str, str]]:
    """Build LLM messages using pinned date from metadata."""
    from config import CHART_THEME
    from llm import DEFAULT_SOUL

    system_content = context.get("soul", DEFAULT_SOUL)
    if system_content == "(not provided)":
        system_content = DEFAULT_SOUL

    pinned_date = date.fromisoformat(metadata["extracted_at"])
    pinned_weekday = metadata["weekday"]

    if week_complete is None:
        week_complete = metadata.get("week_complete", False)

    if week_complete:
        week_status = "This is a full week review (Mon\u2013Sun complete)."
    else:
        week_status = (
            f"This is a mid-week progress check (Mon\u2013{pinned_weekday}). "
            "The week is not over \u2014 do not flag missing sessions for days "
            "that haven\u2019t happened yet."
        )

    template = context["prompt"]
    placeholders: dict[str, str] = defaultdict(lambda: "(not provided)")
    placeholders.update(
        {
            "me": context.get("me", "(not provided)"),
            "goals": context.get("goals", "(not provided)"),
            "plan": context.get("plan", "(not provided)"),
            "log": context.get("log", "(not provided)"),
            "history": context.get("history", "(not provided)"),
            "health_data": json.dumps(health_data, indent=2, default=str),
            "baselines": baselines or context.get("baselines", "(not computed)"),
            "today": pinned_date.isoformat(),
            "weekday": pinned_weekday,
            "week_status": week_status,
            "chart_theme": CHART_THEME,
        }
    )
    for key, value in context.items():
        if key not in placeholders and key not in ("soul", "prompt"):
            placeholders[key] = value
    user_content = template.format_map(placeholders)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    """Outcome of a single assertion check."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalResult:
    """Aggregated outcome of one eval case × model."""

    eval_name: str
    category: str
    model: str
    assertions: list[AssertionResult] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost: float | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(a.passed for a in self.assertions)

    @property
    def failures(self) -> list[AssertionResult]:
        return [a for a in self.assertions if not a.passed]


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _match_pattern(text: str, pattern: str) -> re.Match | None:
    """Match a pattern against text.

    If *pattern* starts with ``re:`` the remainder is used as a regex
    (case-insensitive).  Otherwise it is treated as a plain substring
    match (case-insensitive).
    """
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.IGNORECASE)
    return re.search(re.escape(pattern), text, re.IGNORECASE)


def response_is_skip(text: str) -> AssertionResult:
    """Response is exactly SKIP."""
    if text.strip().upper() == "SKIP":
        return AssertionResult(name="is_skip", passed=True)
    return AssertionResult(
        name="is_skip",
        passed=False,
        detail=f"Expected SKIP, got: {text[:120]}",
    )


def response_is_not_skip(text: str) -> AssertionResult:
    """Response is NOT SKIP."""
    if text.strip().upper() == "SKIP":
        return AssertionResult(
            name="is_not_skip", passed=False, detail="Got unexpected SKIP"
        )
    return AssertionResult(name="is_not_skip", passed=True)


def text_absent(
    text: str, patterns: list[str], name: str = "text_absent"
) -> AssertionResult:
    """Assert that NONE of the patterns match in *text*.

    Returns failure with the first matched pattern and surrounding context.
    """
    for pat in patterns:
        m = _match_pattern(text, pat)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            context = text[start:end].replace("\n", " ")
            label = pat[3:] if pat.startswith("re:") else pat
            return AssertionResult(
                name=name,
                passed=False,
                detail=f"Matched [{label}]: ...{context}...",
            )
    return AssertionResult(name=name, passed=True)


def text_present(
    text: str, patterns: list[str], name: str = "text_present"
) -> AssertionResult:
    """Assert that at least ONE of the patterns matches in *text*."""
    for pat in patterns:
        if _match_pattern(text, pat):
            return AssertionResult(name=name, passed=True)
    labels = [p[3:] if p.startswith("re:") else p for p in patterns]
    return AssertionResult(
        name=name,
        passed=False,
        detail=f"None matched: {labels}",
    )


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_KNOWN_TABLES = {"daily", "workout"}
_KNOWN_COLUMNS = {
    "daily": {
        "date",
        "steps",
        "distance_km",
        "active_energy_kj",
        "exercise_min",
        "stand_hours",
        "flights_climbed",
        "resting_hr",
        "hrv_ms",
        "walking_hr_avg",
        "hr_day_min",
        "hr_day_max",
        "vo2max",
        "walking_speed_kmh",
        "walking_step_length_cm",
        "walking_asymmetry_pct",
        "walking_double_support_pct",
        "stair_speed_up_ms",
        "stair_speed_down_ms",
        "running_stride_length_m",
        "running_power_w",
        "running_speed_kmh",
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
        "recovery_index",
        "imported_at",
    },
    "workout": {
        "start_utc",
        "date",
        "type",
        "category",
        "duration_min",
        "hr_min",
        "hr_avg",
        "hr_max",
        "active_energy_kj",
        "intensity_kcal_per_hr_kg",
        "temperature_c",
        "humidity_pct",
        "gpx_distance_km",
        "gpx_elevation_gain_m",
        "gpx_avg_speed_ms",
        "gpx_max_speed_p95_ms",
        "imported_at",
    },
}

_SQL_KEYWORDS = {
    "select",
    "from",
    "where",
    "and",
    "or",
    "not",
    "in",
    "is",
    "null",
    "as",
    "on",
    "join",
    "left",
    "right",
    "inner",
    "outer",
    "cross",
    "group",
    "by",
    "order",
    "asc",
    "desc",
    "limit",
    "offset",
    "having",
    "union",
    "all",
    "distinct",
    "case",
    "when",
    "then",
    "else",
    "end",
    "between",
    "like",
    "exists",
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "cast",
    "coalesce",
    "ifnull",
    "nullif",
    "round",
    "abs",
    "length",
    "substr",
    "replace",
    "trim",
    "upper",
    "lower",
    "date",
    "time",
    "datetime",
    "strftime",
    "julianday",
    "typeof",
    "total",
    "printf",
    "iif",
    "over",
    "partition",
    "row_number",
    "rank",
    "dense_rank",
    "lag",
    "lead",
    "first_value",
    "last_value",
    "with",
    "recursive",
    "values",
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "index",
    "table",
    "view",
    "trigger",
    "true",
    "false",
    "integer",
    "real",
    "text",
    "blob",
    "primary",
    "key",
    "autoincrement",
    "references",
    "not_tracked",
    "pending",
    "sync_pending",
}


def sql_is_valid_select(query: str) -> AssertionResult:
    """SQL starts with SELECT (ignoring leading whitespace/parens)."""
    stripped = query.strip().lstrip("( \t\n")
    first = stripped.split()[0].upper() if stripped else ""
    if first != "SELECT":
        return AssertionResult(
            name="sql_select", passed=False, detail=f"First keyword: {first}"
        )
    return AssertionResult(name="sql_select", passed=True)


def sql_uses_valid_columns(query: str) -> AssertionResult:
    """SQL references only known schema identifiers (heuristic)."""
    all_columns: set[str] = set()
    for cols in _KNOWN_COLUMNS.values():
        all_columns |= cols
    all_known = all_columns | _KNOWN_TABLES | {"rowid"}

    identifiers = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", query.lower()))
    identifiers -= _SQL_KEYWORDS
    suspect = identifiers - all_known
    if suspect:
        return AssertionResult(
            name="sql_columns",
            passed=False,
            detail=f"Unknown: {sorted(suspect)}",
        )
    return AssertionResult(name="sql_columns", passed=True)


def extract_tool_calls_by_name(tool_calls: list | None, name: str) -> list[dict]:
    """Extract parsed arguments for all tool calls matching *name*."""
    matches: list[dict] = []
    if not tool_calls:
        return matches
    for tc in tool_calls:
        fn = getattr(tc, "function", None) or tc.get("function", {})
        fn_name = getattr(fn, "name", None) or fn.get("name", "")
        if fn_name != name:
            continue
        args_str = getattr(fn, "arguments", None) or fn.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            args = {}
        matches.append(args)
    return matches


def extract_sql_from_tool_calls(tool_calls: list | None) -> list[str]:
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


def build_eval_db(health_data: dict) -> sqlite3.Connection:
    """Build an in-memory SQLite DB from health_data for SQL eval verification.

    Creates ``daily`` and ``workout`` tables and populates them from
    ``health_data["current_week"]["days"]``.
    """
    from store import _DDL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)

    days = health_data.get("current_week", {}).get("days", [])

    # Column names for daily (excluding sleep marker string values).
    daily_cols = [
        "date",
        "steps",
        "distance_km",
        "active_energy_kj",
        "exercise_min",
        "stand_hours",
        "flights_climbed",
        "resting_hr",
        "hrv_ms",
        "walking_hr_avg",
        "hr_day_min",
        "hr_day_max",
        "vo2max",
        "walking_speed_kmh",
        "walking_step_length_cm",
        "walking_asymmetry_pct",
        "walking_double_support_pct",
        "stair_speed_up_ms",
        "stair_speed_down_ms",
        "running_stride_length_m",
        "running_power_w",
        "running_speed_kmh",
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
        "recovery_index",
    ]

    workout_cols = [
        "start_utc",
        "date",
        "type",
        "category",
        "duration_min",
        "hr_min",
        "hr_avg",
        "hr_max",
        "active_energy_kj",
        "intensity_kcal_per_hr_kg",
        "temperature_c",
        "humidity_pct",
        "gpx_distance_km",
        "gpx_elevation_gain_m",
        "gpx_avg_speed_ms",
        "gpx_max_speed_p95_ms",
    ]

    now_iso = "2026-03-28T00:00:00"

    for day in days:
        # Insert daily row.
        vals = []
        for col in daily_cols:
            v = day.get(col)
            # Sleep markers are strings — store as NULL in the DB.
            if isinstance(v, str) and col != "date":
                v = None
            vals.append(v)
        vals.append(now_iso)  # imported_at

        placeholders = ", ".join(["?"] * (len(daily_cols) + 1))
        col_str = ", ".join(daily_cols + ["imported_at"])
        conn.execute(f"INSERT INTO daily ({col_str}) VALUES ({placeholders})", vals)

        # Insert workout rows (inject date from parent day).
        for workout in day.get("workouts", []):
            workout_with_date = {**workout, "date": day["date"]}
            w_vals = [workout_with_date.get(c) for c in workout_cols]
            w_vals.append(now_iso)  # imported_at
            w_placeholders = ", ".join(["?"] * (len(workout_cols) + 1))
            w_col_str = ", ".join(workout_cols + ["imported_at"])
            conn.execute(
                f"INSERT INTO workout ({w_col_str}) VALUES ({w_placeholders})",
                w_vals,
            )

    conn.commit()
    return conn


def execute_sql(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Execute a SQL query and return rows as a list of dicts."""
    try:
        cursor = conn.execute(query)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception as exc:
        return [{"_error": str(exc)}]


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------


def load_cases() -> list[dict]:
    """Load all eval cases from cases.json."""
    return json.loads(_CASES_PATH.read_text(encoding="utf-8"))


def run_case(
    case: dict,
    model: str,
    reasoning_effort: str | None = None,
    progress: Any = None,
    task_id: Any = None,
) -> EvalResult:
    """Run a single eval case: load → scenario → LLM → assertions."""
    from llm import call_llm

    from evals.data.scenarios import ALL_SCENARIOS

    case_id = case["id"]
    category = case["category"]
    config = case.get("config", {})

    if progress and task_id is not None:
        model_short = model.split("/")[-1] if "/" in model else model
        progress.update(
            task_id,
            description=f"[bold]{case_id}[/bold] on {model_short}",
        )

    # Load blueprints.
    prompt_file = config.get("prompt_file", "prompt")
    context, health_data, metadata = load_blueprints(prompt_file)

    # Apply scenario.
    scenario_fn_name = case.get("scenario_fn", "baseline")
    scenario_fn = ALL_SCENARIOS.get(scenario_fn_name, ALL_SCENARIOS["baseline"])
    ctx, hd = scenario_fn(deepcopy(context), deepcopy(health_data))

    # Apply extra context keys.
    for key in ("trigger_type", "recent_nudges"):
        if key in config:
            ctx[key] = config[key]

    # Build messages.
    messages = build_eval_messages(
        ctx,
        hd,
        metadata,
        week_complete=config.get("week_complete"),
    )

    # Extra messages (e.g. chat question for SQL or context_update cases).
    if "question" in case:
        messages.append({"role": "user", "content": case["question"]})

    # Tools.
    tools = None
    if config.get("tools"):
        from tools import all_chat_tools

        tools = all_chat_tools()

    max_tokens = config.get("max_tokens", 4096)

    # Call LLM with temperature=0 for reproducibility.
    t0 = time.perf_counter()
    try:
        result = call_llm(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        if progress and task_id is not None:
            progress.advance(task_id)
        return EvalResult(
            eval_name=case_id,
            category=category,
            model=model,
            error=str(exc),
        )
    latency = time.perf_counter() - t0

    # Run assertions.
    checks = _run_assertions(case, result.text, result.tool_calls, hd)

    if progress and task_id is not None:
        progress.advance(task_id)

    return EvalResult(
        eval_name=case_id,
        category=category,
        model=model,
        assertions=checks,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_s=latency,
        cost=result.cost,
    )


def _run_assertions(
    case: dict,
    response: str,
    tool_calls: list | None,
    health_data: dict,
) -> list[AssertionResult]:
    """Run assertions for a case based on its labelled expectations."""
    results: list[AssertionResult] = []
    category = case["category"]
    expected_action = case.get("expected_action", "any")

    # --- Action-based assertions ---
    if expected_action == "skip":
        results.append(response_is_skip(response))
    elif expected_action == "fire":
        results.append(response_is_not_skip(response))
    # "any", "no_alarm", "no_judgment", "flag", "back_off", "push", "maintain"
    # don't have a skip/fire check — they use must_contain/must_not_contain.

    # --- Pattern assertions ---
    must_not = case.get("must_not_contain", [])
    if must_not:
        results.append(text_absent(response, must_not, name="must_not_contain"))

    must = case.get("must_contain", [])
    if must:
        results.append(text_present(response, must, name="must_contain"))

    # --- SQL-specific assertions ---
    if category == "chat_sql":
        results.extend(_sql_assertions(case, response, tool_calls, health_data))

    # --- Context update assertions ---
    if category == "context_update":
        results.extend(_context_update_assertions(case, tool_calls))

    return results


def _sql_assertions(
    case: dict,
    response: str,
    tool_calls: list | None,
    health_data: dict,
) -> list[AssertionResult]:
    """SQL-specific assertions: valid SQL + execute and verify answer."""
    results: list[AssertionResult] = []
    queries = extract_sql_from_tool_calls(tool_calls)

    # The LLM might answer from context instead of using SQL for current-week
    # questions. Check both paths.
    if not queries:
        # No SQL call — check if the expected value appears in the response.
        expected = case.get("expected_value")
        tolerance = case.get("tolerance", 0)
        if expected is not None:
            # Try to find the expected number in the response text.
            found = _response_contains_value(response, expected, tolerance)
            results.append(
                AssertionResult(
                    name="answer_correct",
                    passed=found,
                    detail=(
                        f"Expected ~{expected} in response (no SQL used)"
                        if not found
                        else f"Found ~{expected} in response (answered from context)"
                    ),
                )
            )
        return results

    results.append(
        AssertionResult(
            name="has_sql",
            passed=True,
            detail=f"{len(queries)} query/queries",
        )
    )

    # Validate each query.
    for i, query in enumerate(queries):
        prefix = f"q{i}" if len(queries) > 1 else "q"

        select_check = sql_is_valid_select(query)
        select_check.name = f"{prefix}_select"
        results.append(select_check)

        col_check = sql_uses_valid_columns(query)
        col_check.name = f"{prefix}_columns"
        results.append(col_check)

    # Execute the first query and validate the answer.
    expected = case.get("expected_value")
    tolerance = case.get("tolerance", 0)
    if expected is not None and queries:
        conn = build_eval_db(health_data)
        try:
            rows = execute_sql(conn, queries[0])
            if rows and "_error" in rows[0]:
                results.append(
                    AssertionResult(
                        name="sql_executes",
                        passed=False,
                        detail=f"SQL error: {rows[0]['_error']}",
                    )
                )
            elif rows:
                # Check if any value in the first row is close to expected.
                first_row = rows[0]
                matched = False
                for val in first_row.values():
                    if val is None:
                        continue
                    try:
                        if abs(float(val) - float(expected)) <= tolerance:
                            matched = True
                            break
                    except (ValueError, TypeError):
                        continue
                results.append(
                    AssertionResult(
                        name="answer_correct",
                        passed=matched,
                        detail=(
                            f"Expected ~{expected}, got row: {dict(first_row)}"
                            if not matched
                            else f"Correct: ~{expected}"
                        ),
                    )
                )
            else:
                results.append(
                    AssertionResult(
                        name="answer_correct",
                        passed=False,
                        detail="Query returned no rows",
                    )
                )
        finally:
            conn.close()

    return results


def _context_update_assertions(
    case: dict, tool_calls: list | None
) -> list[AssertionResult]:
    """Check whether update_context was called (or not) as expected."""
    results: list[AssertionResult] = []
    expected_tool = case.get("expected_tool")
    calls = extract_tool_calls_by_name(tool_calls, "update_context")

    if expected_tool is None:
        # Should NOT have called update_context.
        if calls:
            results.append(
                AssertionResult(
                    name="no_update_context",
                    passed=False,
                    detail=f"Unexpected update_context call: file={calls[0].get('file')}",
                )
            )
        else:
            results.append(AssertionResult(name="no_update_context", passed=True))
        return results

    if expected_tool == "update_context":
        if not calls:
            results.append(
                AssertionResult(
                    name="has_update_context",
                    passed=False,
                    detail="Expected update_context call, got none",
                )
            )
            return results

        results.append(AssertionResult(name="has_update_context", passed=True))

        # Check file target if specified.
        expected_args = case.get("expected_tool_args", {})
        expected_files = expected_args.get("file", [])
        if expected_files:
            actual_files = [c.get("file", "") for c in calls]
            if any(f in expected_files for f in actual_files):
                results.append(
                    AssertionResult(
                        name="correct_file",
                        passed=True,
                        detail=f"Updated: {actual_files}",
                    )
                )
            else:
                results.append(
                    AssertionResult(
                        name="correct_file",
                        passed=False,
                        detail=f"Expected file in {expected_files}, got {actual_files}",
                    )
                )

    return results


def _response_contains_value(
    text: str, expected: float | int, tolerance: float
) -> bool:
    """Check if the expected numeric value appears in the response text."""
    numbers = re.findall(r"[\d]+\.?\d*", text)
    for n in numbers:
        try:
            if abs(float(n) - float(expected)) <= tolerance:
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Results display
# ---------------------------------------------------------------------------


def print_results(results: list[EvalResult]) -> None:
    """Print a rich table of eval results."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Eval Results", show_lines=True)
    table.add_column("Case", style="bold")
    table.add_column("Category")
    table.add_column("Model")
    table.add_column("Pass", justify="center")
    table.add_column("Checks", justify="right")
    table.add_column("Tokens (in/out)", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Failures")

    for r in results:
        if r.error:
            pass_str = "[red]ERR[/red]"
            fail_str = r.error[:80]
            checks_str = "-"
        else:
            total = len(r.assertions)
            passed = sum(1 for a in r.assertions if a.passed)
            pass_str = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
            checks_str = f"{passed}/{total}"
            fail_str = "; ".join(
                f.name + (f": {f.detail}" if f.detail else "") for f in r.failures
            )

        model_short = r.model.split("/")[-1] if "/" in r.model else r.model

        table.add_row(
            r.eval_name,
            r.category,
            model_short,
            pass_str,
            checks_str,
            f"{r.input_tokens}/{r.output_tokens}",
            f"{r.latency_s:.1f}s",
            f"${r.cost:.4f}" if r.cost else "-",
            fail_str or "-",
        )

    console.print(table)

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    total_cost = sum(r.cost or 0.0 for r in results)
    console.print(
        f"\n{passed}/{total} passed"
        + (f"  |  total cost: ${total_cost:.4f}" if total_cost else "")
    )

    _print_model_chart(results, console)


def _print_model_chart(results: list[EvalResult], console: Any) -> None:
    """Print ASCII bar charts comparing models side-by-side."""
    from rich.panel import Panel
    from rich.text import Text

    model_stats: dict[str, dict] = {}
    for r in results:
        short = r.model.split("/")[-1] if "/" in r.model else r.model
        if short not in model_stats:
            model_stats[short] = {
                "total": 0,
                "passed": 0,
                "latency": [],
                "cost": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
            }
        s = model_stats[short]
        s["total"] += 1
        s["passed"] += 1 if r.passed else 0
        if r.latency_s > 0:
            s["latency"].append(r.latency_s)
        s["cost"] += r.cost or 0.0
        s["tokens_in"] += r.input_tokens
        s["tokens_out"] += r.output_tokens

    if not model_stats:
        return

    BAR_WIDTH = 30
    label_width = max(len(m) for m in model_stats) + 1

    lines = Text()

    # Pass rate.
    lines.append("  Pass Rate\n", style="bold")
    for model_name, s in model_stats.items():
        rate = s["passed"] / s["total"] if s["total"] else 0
        filled = round(rate * BAR_WIDTH)
        color = "green" if rate == 1.0 else "yellow" if rate >= 0.5 else "red"
        lines.append(f"  {model_name:>{label_width}} ")
        lines.append("\u2588" * filled, style=color)
        lines.append("\u2591" * (BAR_WIDTH - filled), style="dim")
        lines.append(f" {s['passed']}/{s['total']} ({rate:.0%})\n")

    # Avg latency.
    max_lat = max(
        (sum(s["latency"]) / len(s["latency"]) if s["latency"] else 0)
        for s in model_stats.values()
    )
    if max_lat > 0:
        lines.append("\n  Avg Latency\n", style="bold")
        for model_name, s in model_stats.items():
            avg = sum(s["latency"]) / len(s["latency"]) if s["latency"] else 0
            filled = round((avg / max_lat) * BAR_WIDTH) if max_lat else 0
            color = (
                "green"
                if avg <= max_lat * 0.5
                else "yellow"
                if avg <= max_lat * 0.8
                else "red"
            )
            lines.append(f"  {model_name:>{label_width}} ")
            lines.append("\u2588" * filled, style=color)
            lines.append("\u2591" * (BAR_WIDTH - filled), style="dim")
            lines.append(f" {avg:.1f}s\n")

    # Cost.
    max_cost = max(s["cost"] for s in model_stats.values())
    if max_cost > 0:
        lines.append("\n  Total Cost\n", style="bold")
        for model_name, s in model_stats.items():
            filled = round((s["cost"] / max_cost) * BAR_WIDTH) if max_cost else 0
            color = (
                "green"
                if s["cost"] <= max_cost * 0.3
                else "yellow"
                if s["cost"] <= max_cost * 0.7
                else "red"
            )
            lines.append(f"  {model_name:>{label_width}} ")
            lines.append("\u2588" * filled, style=color)
            lines.append("\u2591" * (BAR_WIDTH - filled), style="dim")
            lines.append(f" ${s['cost']:.4f}\n")

    # Tokens.
    max_tok = max(s["tokens_in"] + s["tokens_out"] for s in model_stats.values())
    if max_tok > 0:
        lines.append("\n  Tokens (in+out)\n", style="bold")
        for model_name, s in model_stats.items():
            tok = s["tokens_in"] + s["tokens_out"]
            filled = round((tok / max_tok) * BAR_WIDTH) if max_tok else 0
            lines.append(f"  {model_name:>{label_width}} ")
            lines.append("\u2588" * filled, style="cyan")
            lines.append("\u2591" * (BAR_WIDTH - filled), style="dim")
            lines.append(f" {tok:,}\n")

    console.print(
        Panel(lines, title="Model Comparison", border_style="dim", expand=False)
    )
