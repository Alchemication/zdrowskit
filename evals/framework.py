"""Eval framework — blueprint loading, case runner, assertion helpers, results.

The eval system is driven by ``evals/data/cases.json``, a human-labelled
dataset of test cases.  Each case specifies a scenario, config, and
expectations (must_contain / must_not_contain patterns, expected actions,
or SQL answer validation).

Building blocks:
    load_blueprints()     — read pinned context + health data
    load_cases()          — load and validate case schema
    run_case()            — load → scenario → LLM call/tool loop → assertions
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
_BASELINE_BLUEPRINT = "baseline"

DEFAULT_MODEL = "anthropic/claude-opus-4-6"


# ---------------------------------------------------------------------------
# Blueprint loading
# ---------------------------------------------------------------------------

_CONTEXT_STEMS = ["me", "strategy", "log", "history", "baselines"]


def load_blueprints(
    prompt_file: str = "prompt",
    blueprint: str = _BASELINE_BLUEPRINT,
) -> tuple[dict[str, str], dict, dict, dict | None]:
    """Load pinned context, health data, and metadata from committed blueprints.

    Prompt templates are loaded live from ``src/prompts/`` (version-controlled
    code).  User context files come from the blueprint snapshot.

    Args:
        prompt_file: Prompt template stem loaded from ``src/prompts``.
        blueprint: Named blueprint directory, or ``"baseline"`` for the
            top-level pinned snapshot.

    Returns:
        A ``(context, health_data, metadata, db_seed)`` tuple.
    """
    from config import PROMPTS_DIR

    bp_dir = _resolve_blueprint_dir(blueprint)
    base_dir = _resolve_blueprint_dir(_BASELINE_BLUEPRINT)
    context: dict[str, str] = {}

    prompt_path = PROMPTS_DIR / f"{prompt_file}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt template missing: {prompt_path}")
    context["prompt"] = prompt_path.read_text(encoding="utf-8")

    soul_path = PROMPTS_DIR / "soul.md"
    if soul_path.exists():
        context["soul"] = soul_path.read_text(encoding="utf-8")

    ctx_dir = bp_dir / "context"
    fallback_ctx_dir = base_dir / "context"
    for stem in _CONTEXT_STEMS:
        path = ctx_dir / f"{stem}.md"
        if not path.exists():
            path = fallback_ctx_dir / f"{stem}.md"
        context[stem] = (
            path.read_text(encoding="utf-8") if path.exists() else "(not provided)"
        )

    hd_path = bp_dir / "health_data.json"
    health_data = json.loads(hd_path.read_text(encoding="utf-8"))

    meta_path = bp_dir / "metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))

    db_seed_path = bp_dir / "db_seed.json"
    db_seed = None
    if db_seed_path.exists():
        db_seed = json.loads(db_seed_path.read_text(encoding="utf-8"))

    return context, health_data, metadata, db_seed


def _resolve_blueprint_dir(blueprint: str) -> Path:
    """Return the filesystem directory for a named blueprint.

    Args:
        blueprint: Blueprint name from an eval case.

    Returns:
        Filesystem path for the requested blueprint.

    Raises:
        FileNotFoundError: If the named blueprint does not exist.
    """
    if (
        blueprint == _BASELINE_BLUEPRINT
        and (_BLUEPRINTS_DIR / "health_data.json").exists()
    ):
        return _BLUEPRINTS_DIR
    path = _BLUEPRINTS_DIR / blueprint
    if not path.exists():
        raise FileNotFoundError(f"Unknown blueprint: {blueprint}")
    return path


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
    suite: str
    category: str
    model: str
    assertions: list[AssertionResult] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost: float | None = None
    cached: bool = False
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(a.passed for a in self.assertions)

    @property
    def failures(self) -> list[AssertionResult]:
        return [a for a in self.assertions if not a.passed]


@dataclass
class EvalExecution:
    """Outcome of one eval execution path before assertions."""

    text: str
    tool_calls: list = field(default_factory=list)
    query_rows: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost: float | None = None


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


def build_eval_db(
    health_data: dict,
    db_seed: dict | None = None,
) -> sqlite3.Connection:
    """Build an in-memory SQLite DB from health_data for SQL eval verification.

    Creates ``daily`` and ``workout`` tables and populates them from
    ``db_seed["days"]`` when present, otherwise from
    ``health_data["current_week"]["days"]``.
    """
    from db.migrations import apply_migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)

    seed_days = (db_seed or {}).get("days", [])
    days = seed_days or health_data.get("current_week", {}).get("days", [])

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
    """Load and validate all eval cases from cases.json.

    Returns:
        List of validated case dicts.

    Raises:
        ValueError: If the file is empty, malformed, or contains invalid cases.
    """
    cases = json.loads(_CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.json must contain a non-empty list")
    seen_ids: set[str] = set()
    for case in cases:
        _validate_case_schema(case)
        case_id = case["id"]
        if case_id in seen_ids:
            raise ValueError(f"Duplicate eval case id: {case_id}")
        seen_ids.add(case_id)
    return cases


def _validate_case_schema(case: dict) -> None:
    """Validate the minimal structure of an eval case.

    Args:
        case: Parsed eval case dict from ``cases.json``.

    Raises:
        ValueError: If the case does not match the expected schema.
    """
    required = {"id", "suite", "category", "scenario_fn", "config", "assertions"}
    missing = sorted(required - set(case))
    if missing:
        raise ValueError(f"Case missing required fields {missing}: {case}")

    if case["suite"] not in {"core", "benchmark"}:
        raise ValueError(f"Invalid suite for {case['id']}: {case['suite']}")

    if not isinstance(case["assertions"], list) or not case["assertions"]:
        raise ValueError(f"Case must define non-empty assertions: {case['id']}")

    if "question" in case and "turns" in case:
        raise ValueError(f"Case cannot define both question and turns: {case['id']}")

    if "turns" in case:
        turns = case["turns"]
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"turns must be a non-empty list: {case['id']}")
        if turns[-1].get("role") != "user":
            raise ValueError(f"Final turn must be a user message: {case['id']}")
        for turn in turns:
            if turn.get("role") not in {"user", "assistant"} or not turn.get("content"):
                raise ValueError(f"Invalid turn in case {case['id']}: {turn}")

    for assertion in case["assertions"]:
        if not isinstance(assertion, dict) or not assertion.get("type"):
            raise ValueError(f"Invalid assertion in case {case['id']}: {assertion}")


def run_case(
    case: dict,
    model: str,
    reasoning_effort: str | None = None,
    progress: Any = None,
    task_id: Any = None,
    use_cache: bool = True,
) -> EvalResult:
    """Run a single eval case: load → scenario → LLM/tool loop → assertions.

    Args:
        case: Eval case definition.
        model: Model string forwarded to ``call_llm``.
        reasoning_effort: Optional reasoning effort hint for the model.
        progress: Optional rich progress instance for CLI updates.
        task_id: Optional progress task id.
        use_cache: Whether to read/write the shared eval cache.

    Returns:
        Aggregated result for the case/model pair.
    """
    from llm import build_messages, call_llm, render_health_data

    from evals.data.scenarios import ALL_SCENARIOS

    case_id = case["id"]
    suite = case["suite"]
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
    blueprint = case.get("blueprint", _BASELINE_BLUEPRINT)
    context, health_data, metadata, db_seed = load_blueprints(prompt_file, blueprint)

    # Apply scenario.
    scenario_fn_name = case.get("scenario_fn", "baseline")
    scenario_fn = ALL_SCENARIOS.get(scenario_fn_name, ALL_SCENARIOS["baseline"])
    ctx, hd = scenario_fn(deepcopy(context), deepcopy(health_data))

    # Apply extra context keys.
    for key in ("trigger_type", "recent_nudges"):
        if key in config:
            ctx[key] = config[key]

    # Build messages using the production build_messages with pinned date.
    pinned_date = date.fromisoformat(metadata["extracted_at"])
    wc = config.get("week_complete", metadata.get("week_complete", False))
    prompt_kind = {
        "prompt": "report",
        "chat_prompt": "chat",
        "coach_prompt": "coach",
        "nudge_prompt": "nudge",
    }.get(prompt_file, "report")
    messages = build_messages(
        ctx,
        health_data_text=render_health_data(
            hd,
            prompt_kind=prompt_kind,
            week=config.get("week", "current"),
            today=pinned_date,
        ),
        baselines=ctx.get("baselines", "(not computed)"),
        week_complete=wc,
        today=pinned_date,
    )

    # Conversation turns, if any, come after the system/context prompt.
    messages.extend(_case_turns(case))

    # Tools.
    tools = None
    if config.get("tools"):
        from tools import all_chat_tools

        tools = all_chat_tools()

    max_tokens = config.get("max_tokens", 4096)
    temperature = 0.0
    max_tool_iterations = case.get(
        "max_tool_iterations",
        config.get("max_tool_iterations", 5),
    )
    cache_key_hash: str | None = None
    cache_key_json: str | None = None

    if use_cache:
        from evals.cache import build_cache_key, load_cache_entry

        cache_payload = _build_cache_payload(
            case=case,
            model=model,
            reasoning_effort=reasoning_effort,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            max_tool_iterations=max_tool_iterations,
        )
        cache_key_hash, cache_key_json = build_cache_key(cache_payload)
        cached = load_cache_entry(cache_key_hash)
        if cached is not None:
            execution = EvalExecution(
                text=cached.response_text,
                tool_calls=cached.tool_calls,
                query_rows=cached.query_rows,
                input_tokens=cached.input_tokens,
                output_tokens=cached.output_tokens,
                latency_s=cached.latency_s,
                cost=cached.cost,
            )
            checks = _run_assertions(
                case=case,
                response=execution.text,
                tool_calls=execution.tool_calls,
                health_data=hd,
                db_seed=db_seed,
                query_rows=execution.query_rows,
            )
            if progress and task_id is not None:
                progress.advance(task_id)
            return EvalResult(
                eval_name=case_id,
                suite=suite,
                category=category,
                model=model,
                assertions=checks,
                input_tokens=execution.input_tokens,
                output_tokens=execution.output_tokens,
                latency_s=execution.latency_s,
                cost=execution.cost,
                cached=True,
            )

    # Call LLM with temperature=0 for reproducibility.
    t0 = time.perf_counter()
    try:
        if tools:
            execution = _run_chat_eval(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                health_data=hd,
                db_seed=db_seed,
                max_tool_iterations=max_tool_iterations,
            )
        else:
            result = call_llm(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                reasoning_effort=reasoning_effort,
            )
            execution = EvalExecution(
                text=result.text,
                tool_calls=result.tool_calls or [],
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_s=getattr(result, "latency_s", 0.0),
                cost=getattr(result, "cost", None),
            )
    except Exception as exc:
        if progress and task_id is not None:
            progress.advance(task_id)
        return EvalResult(
            eval_name=case_id,
            suite=suite,
            category=category,
            model=model,
            error=str(exc),
        )
    latency = execution.latency_s or (time.perf_counter() - t0)

    if use_cache and cache_key_hash is not None and cache_key_json is not None:
        from evals.cache import save_cache_entry

        save_cache_entry(
            key_hash=cache_key_hash,
            key_json=cache_key_json,
            case_id=case_id,
            suite=suite,
            category=category,
            model=model,
            response_text=execution.text,
            tool_calls=_serialise_tool_calls(execution.tool_calls),
            query_rows=execution.query_rows,
            input_tokens=execution.input_tokens,
            output_tokens=execution.output_tokens,
            latency_s=latency,
            cost=execution.cost,
        )

    # Run assertions.
    checks = _run_assertions(
        case=case,
        response=execution.text,
        tool_calls=execution.tool_calls,
        health_data=hd,
        db_seed=db_seed,
        query_rows=execution.query_rows,
    )

    if progress and task_id is not None:
        progress.advance(task_id)

    return EvalResult(
        eval_name=case_id,
        suite=suite,
        category=category,
        model=model,
        assertions=checks,
        input_tokens=execution.input_tokens,
        output_tokens=execution.output_tokens,
        latency_s=latency,
        cost=execution.cost,
    )


def _build_cache_payload(
    *,
    case: dict,
    model: str,
    reasoning_effort: str | None,
    messages: list[dict[str, str]],
    tools: list[dict] | None,
    max_tokens: int,
    temperature: float,
    max_tool_iterations: int,
) -> dict:
    """Build the strong cache-key payload for an eval execution.

    Args:
        case: Eval case definition.
        model: Model string forwarded to ``call_llm``.
        reasoning_effort: Optional reasoning effort hint.
        messages: Rendered eval messages.
        tools: Tool schema used for the case, if any.
        max_tokens: Maximum tokens per LLM call.
        temperature: Sampling temperature.
        max_tool_iterations: Tool-loop iteration cap for chat evals.

    Returns:
        JSON-serialisable cache-key payload.
    """
    return {
        "cache_version": 1,
        "case_id": case["id"],
        "suite": case["suite"],
        "category": case["category"],
        "blueprint": case.get("blueprint", _BASELINE_BLUEPRINT),
        "scenario_fn": case.get("scenario_fn", "baseline"),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "max_tool_iterations": max_tool_iterations,
        "messages": messages,
        "tools": tools,
    }


def _serialise_tool_calls(tool_calls: list | None) -> list[dict]:
    """Convert tool calls into a JSON-serialisable list.

    Args:
        tool_calls: Tool calls emitted during execution.

    Returns:
        Serialised tool-call dicts.
    """
    serialised: list[dict] = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None) or tc.get("function", {})
        serialised.append(
            {
                "id": getattr(tc, "id", None) or tc.get("id", ""),
                "type": getattr(tc, "type", None) or tc.get("type", "function"),
                "function": {
                    "name": getattr(fn, "name", None) or fn.get("name", ""),
                    "arguments": getattr(fn, "arguments", None)
                    or fn.get("arguments", "{}"),
                },
            }
        )
    return serialised


def _case_turns(case: dict) -> list[dict[str, str]]:
    """Return chat turns for a case.

    Args:
        case: Eval case definition.

    Returns:
        Conversation turns in litellm message format.
    """
    if "turns" in case:
        return [
            {"role": turn["role"], "content": turn["content"]} for turn in case["turns"]
        ]
    if "question" in case:
        return [{"role": "user", "content": case["question"]}]
    return []


def _run_chat_eval(
    messages: list[dict[str, str]],
    model: str,
    max_tokens: int,
    reasoning_effort: str | None,
    health_data: dict,
    db_seed: dict | None,
    max_tool_iterations: int,
) -> EvalExecution:
    """Run a daemon-style chat tool loop for eval cases.

    Args:
        messages: Seed messages, including system prompt and conversation turns.
        model: Model string forwarded to ``call_llm``.
        max_tokens: Maximum tokens per model call.
        reasoning_effort: Optional reasoning effort hint.
        health_data: Prompt-visible health data for the current case.
        db_seed: Optional extended DB seed used for SQL-backed chat evals.
        max_tool_iterations: Hard cap on tool loop iterations.

    Returns:
        Execution result containing final text, tool calls, query rows, and
        usage totals across the loop.
    """
    from llm import call_llm
    from tools import all_chat_tools

    all_messages = list(messages)
    all_tool_calls: list = []
    query_rows: list[dict] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_latency_s = 0.0
    total_cost = 0.0
    saw_cost = False

    conn = build_eval_db(health_data, db_seed=db_seed)
    try:
        for _iteration in range(max_tool_iterations):
            result = call_llm(
                messages=all_messages,
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                tools=all_chat_tools(),
                reasoning_effort=reasoning_effort,
            )
            total_input_tokens += result.input_tokens
            total_output_tokens += result.output_tokens
            total_latency_s += getattr(result, "latency_s", 0.0)
            if getattr(result, "cost", None) is not None:
                total_cost += result.cost
                saw_cost = True

            if not result.tool_calls:
                return EvalExecution(
                    text=result.text,
                    tool_calls=all_tool_calls,
                    query_rows=query_rows,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    latency_s=total_latency_s,
                    cost=total_cost if saw_cost else None,
                )

            all_messages.append(result.raw_message)
            all_tool_calls.extend(result.tool_calls)

            for tc in result.tool_calls:
                fn = getattr(tc, "function", None) or tc.get("function", {})
                fn_name = getattr(fn, "name", None) or fn.get("name", "")
                raw_args = getattr(fn, "arguments", None) or fn.get("arguments", "{}")
                try:
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except (json.JSONDecodeError, TypeError):
                    args = {}

                if fn_name == "run_sql":
                    tool_result = _execute_eval_run_sql(conn, args)
                    try:
                        parsed = json.loads(tool_result)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, list):
                        query_rows.extend(parsed)
                elif fn_name == "update_context":
                    tool_result = "Proposed. User will be asked to confirm."
                else:
                    tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})

                all_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": getattr(tc, "id", None) or tc.get("id", ""),
                        "content": tool_result,
                    }
                )
    finally:
        conn.close()

    raise RuntimeError(
        f"Tool loop exhausted after {max_tool_iterations} iterations "
        "without a final text response"
    )


def _execute_eval_run_sql(conn: sqlite3.Connection, arguments: dict) -> str:
    """Execute a run_sql-like tool call against the eval DB.

    Args:
        conn: Open eval SQLite connection.
        arguments: Parsed tool-call arguments.

    Returns:
        JSON string with rows on success or an ``error`` object on failure.
    """
    query = str(arguments.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "Empty query."})

    if not sql_is_valid_select(query).passed:
        return json.dumps({"error": "Only SELECT queries are allowed."})

    try:
        limit = min(int(arguments.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50

    wrapped = f"SELECT * FROM ({query}) LIMIT {limit}"
    rows = execute_sql(conn, wrapped)
    if rows and "_error" in rows[0]:
        return json.dumps({"error": rows[0]["_error"]})
    return json.dumps(rows, default=str)


def _run_assertions(
    case: dict,
    response: str,
    tool_calls: list | None,
    health_data: dict,
    db_seed: dict | None,
    query_rows: list[dict] | None,
) -> list[AssertionResult]:
    """Run typed assertions for a case.

    Args:
        case: Eval case definition.
        response: Final model response text.
        tool_calls: Tool calls emitted during execution.
        health_data: Prompt-visible health data for the case.
        db_seed: Optional extended DB seed used for SQL/chart assertions.
        query_rows: Query rows accumulated during the tool loop.

    Returns:
        Assertion results in case order.
    """
    results: list[AssertionResult] = []
    for assertion in case["assertions"]:
        results.append(
            _evaluate_assertion(
                assertion=assertion,
                response=response,
                tool_calls=tool_calls,
                health_data=health_data,
                db_seed=db_seed,
                query_rows=query_rows or [],
            )
        )
    return results


def _evaluate_assertion(
    assertion: dict,
    response: str,
    tool_calls: list | None,
    health_data: dict,
    db_seed: dict | None,
    query_rows: list[dict],
) -> AssertionResult:
    """Evaluate one typed assertion.

    Args:
        assertion: Assertion definition from a case.
        response: Final model response text.
        tool_calls: Tool calls emitted during execution.
        health_data: Prompt-visible health data for the case.
        db_seed: Optional extended DB seed used for SQL/chart assertions.
        query_rows: Query rows accumulated during the tool loop.

    Returns:
        Outcome of the evaluated assertion.
    """
    atype = assertion["type"]
    name = assertion.get("name", atype)

    if atype == "response_is_skip":
        result = response_is_skip(response)
    elif atype == "response_is_not_skip":
        result = response_is_not_skip(response)
    elif atype == "text_present":
        result = text_present(response, assertion.get("patterns", []), name=name)
    elif atype == "text_absent":
        result = text_absent(response, assertion.get("patterns", []), name=name)
    elif atype == "no_tool_called":
        result = _assert_no_tool_called(tool_calls, assertion)
    elif atype == "tool_called":
        result = _assert_tool_called(tool_calls, assertion)
    elif atype == "tool_arg_matches":
        result = _assert_tool_arg_matches(tool_calls, assertion)
    elif atype == "word_count_max":
        result = _assert_word_count_max(response, assertion)
    elif atype == "required_sections":
        result = _assert_required_sections(response, assertion)
    elif atype == "contains_memory_block":
        result = _assert_contains_memory_block(response, assertion)
    elif atype == "pace_format_valid":
        result = _assert_pace_format_valid(response)
    elif atype == "no_markdown_table":
        result = _assert_no_markdown_table(response)
    elif atype == "chart_count":
        result = _assert_chart_count(response, assertion)
    elif atype == "chart_renders":
        result = _assert_chart_renders(
            response, tool_calls, health_data, db_seed, query_rows
        )
    elif atype == "honest_when_unknown":
        result = _assert_honest_when_unknown(response, assertion)
    elif atype == "answer_uses_expected_value":
        result = _assert_answer_uses_expected_value(response, assertion)
    elif atype == "answer_does_not_contradict_data":
        result = text_absent(response, assertion.get("patterns", []), name=name)
    elif atype == "sql_valid":
        result = _assert_sql_valid(tool_calls)
    else:
        result = AssertionResult(
            name=name,
            passed=False,
            detail=f"Unknown assertion type: {atype}",
        )

    result.name = name
    return result


def _assert_no_tool_called(tool_calls: list | None, assertion: dict) -> AssertionResult:
    """Assert that a tool was not called.

    Args:
        tool_calls: Tool calls emitted during execution.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    tool = assertion.get("tool")
    if not tool_calls:
        return AssertionResult(name="no_tool_called", passed=True)
    if tool is None:
        return AssertionResult(
            name="no_tool_called",
            passed=False,
            detail=f"Unexpected tool calls: {len(tool_calls)}",
        )
    calls = extract_tool_calls_by_name(tool_calls, tool)
    if calls:
        return AssertionResult(
            name="no_tool_called",
            passed=False,
            detail=f"Unexpected {tool} call(s): {len(calls)}",
        )
    return AssertionResult(name="no_tool_called", passed=True)


def _assert_tool_called(tool_calls: list | None, assertion: dict) -> AssertionResult:
    """Assert that a tool was called a certain number of times.

    Args:
        tool_calls: Tool calls emitted during execution.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    tool = assertion["tool"]
    calls = extract_tool_calls_by_name(tool_calls, tool)
    count = len(calls)
    min_count = assertion.get("min_count", 1)
    max_count = assertion.get("max_count")
    passed = count >= min_count and (max_count is None or count <= max_count)
    detail = f"{tool}: {count} call(s)"
    return AssertionResult(name="tool_called", passed=passed, detail=detail)


def _assert_tool_arg_matches(
    tool_calls: list | None, assertion: dict
) -> AssertionResult:
    """Assert that any call for a tool matches the provided args.

    Args:
        tool_calls: Tool calls emitted during execution.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    tool = assertion["tool"]
    matches = assertion.get("matches", {})
    calls = extract_tool_calls_by_name(tool_calls, tool)
    for call in calls:
        if all(
            _arg_matches(call.get(key), expected) for key, expected in matches.items()
        ):
            return AssertionResult(
                name="tool_arg_matches",
                passed=True,
                detail=f"Matched {tool}: {matches}",
            )
    return AssertionResult(
        name="tool_arg_matches",
        passed=False,
        detail=f"No {tool} call matched {matches}; got {calls}",
    )


def _arg_matches(actual: Any, expected: Any) -> bool:
    """Return True when a tool arg matches its expected value.

    Args:
        actual: Actual tool-call argument value.
        expected: Expected assertion value.

    Returns:
        True if the values match under the assertion rules.
    """
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def _assert_word_count_max(response: str, assertion: dict) -> AssertionResult:
    """Assert a maximum word count.

    Args:
        response: Final model response text.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    max_words = assertion["max_words"]
    words = len(re.findall(r"\S+", response.strip()))
    return AssertionResult(
        name="word_count_max",
        passed=words <= max_words,
        detail=f"{words} words",
    )


def _assert_required_sections(response: str, assertion: dict) -> AssertionResult:
    """Assert that required section labels appear in the response.

    Args:
        response: Final model response text.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    missing = [
        section
        for section in assertion.get("sections", [])
        if section.lower() not in response.lower()
    ]
    return AssertionResult(
        name="required_sections",
        passed=not missing,
        detail="" if not missing else f"Missing: {missing}",
    )


def _assert_contains_memory_block(response: str, assertion: dict) -> AssertionResult:
    """Assert that a memory block exists with enough bullet points.

    Args:
        response: Final model response text.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    match = re.search(r"<memory>(.*?)</memory>", response, re.DOTALL | re.IGNORECASE)
    if not match:
        return AssertionResult(
            name="contains_memory_block",
            passed=False,
            detail="No <memory> block found",
        )
    body = match.group(1)
    min_bullets = assertion.get("min_bullets", 1)
    bullets = re.findall(r"(?m)^\s*[-*]\s+", body)
    return AssertionResult(
        name="contains_memory_block",
        passed=len(bullets) >= min_bullets,
        detail=f"{len(bullets)} bullet(s)",
    )


def _assert_pace_format_valid(response: str) -> AssertionResult:
    """Assert that pace uses valid ``mm:ss/km`` formatting.

    Args:
        response: Final model response text.

    Returns:
        Assertion outcome.
    """
    bad_patterns = [
        r"\b\d+\.\d+\s*/\s*km\b",
        r"\b\d+\.\d+\s*min(?:ute)?s?\s*/?\s*km\b",
        r"\bpace\s+\d+\.\d+\b",
    ]
    for pattern in bad_patterns:
        if re.search(pattern, response, re.IGNORECASE):
            return AssertionResult(
                name="pace_format_valid",
                passed=False,
                detail=f"Matched bad pace format: {pattern}",
            )
    for match in re.finditer(r"\b(\d{1,2}):(\d{2})\s*/\s*km\b", response):
        seconds = int(match.group(2))
        if seconds >= 60:
            return AssertionResult(
                name="pace_format_valid",
                passed=False,
                detail=f"Invalid pace seconds in: {match.group(0)}",
            )
    return AssertionResult(name="pace_format_valid", passed=True)


def _assert_no_markdown_table(response: str) -> AssertionResult:
    """Assert that the response does not contain markdown tables.

    Looks for a separator row (``| --- | --- |``) which is required in
    valid markdown tables, avoiding false positives on lines that happen
    to contain pipe characters.

    Args:
        response: Final model response text.

    Returns:
        Assertion outcome.
    """
    if re.search(r"^\s*\|[\s:-]+\|", response, re.MULTILINE):
        for line in response.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and stripped.count("|") >= 3:
                return AssertionResult(
                    name="no_markdown_table",
                    passed=False,
                    detail=f"Looks like a table row: {line[:80]}",
                )
    return AssertionResult(name="no_markdown_table", passed=True)


def _assert_chart_count(response: str, assertion: dict) -> AssertionResult:
    """Assert the number of chart blocks in the response.

    Args:
        response: Final model response text.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    from charts import extract_charts

    count = len(extract_charts(response))
    min_count = assertion.get("min_count", 0)
    max_count = assertion.get("max_count")
    exact = assertion.get("count")
    if exact is not None:
        passed = count == exact
    else:
        passed = count >= min_count and (max_count is None or count <= max_count)
    return AssertionResult(
        name="chart_count",
        passed=passed,
        detail=f"{count} chart(s)",
    )


def _assert_honest_when_unknown(response: str, assertion: dict) -> AssertionResult:
    """Assert that the answer is candid when data is unavailable.

    Args:
        response: Final model response text.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    patterns = assertion.get(
        "patterns",
        [
            "re:(?i)(don'?t|do not)\\s+(have|see)",
            "re:(?i)(can'?t|cannot)\\s+see",
            "re:(?i)not\\s+in\\s+the\\s+data",
            "re:(?i)no\\s+data",
        ],
    )
    return text_present(response, patterns, name="honest_when_unknown")


def _assert_answer_uses_expected_value(
    response: str,
    assertion: dict,
) -> AssertionResult:
    """Assert that the response includes the expected numeric value.

    Args:
        response: Final model response text.
        assertion: Assertion definition.

    Returns:
        Assertion outcome.
    """
    expected = assertion["expected"]
    tolerance = assertion.get("tolerance", 0)
    found = _response_contains_value(response, expected, tolerance)
    return AssertionResult(
        name="answer_uses_expected_value",
        passed=found,
        detail=(
            f"Expected ~{expected} in response"
            if not found
            else f"Found ~{expected} in response"
        ),
    )


def _response_contains_value(
    text: str, expected: float | int, tolerance: float
) -> bool:
    """Check if the expected numeric value appears in the response text.

    Args:
        text: Response text to inspect.
        expected: Expected numeric value.
        tolerance: Allowed numeric delta.

    Returns:
        True if the expected value is present within tolerance.
    """
    numbers = re.findall(r"[\d]+\.?\d*", text)
    for n in numbers:
        try:
            if abs(float(n) - float(expected)) <= tolerance:
                return True
        except ValueError:
            continue
    return False


def _assert_sql_valid(tool_calls: list | None) -> AssertionResult:
    """Assert that all run_sql tool calls use valid SELECT queries.

    Combines ``sql_is_valid_select`` and ``sql_uses_valid_columns`` checks
    across every ``run_sql`` call in the execution.

    Args:
        tool_calls: Tool calls emitted during execution.

    Returns:
        Assertion outcome.
    """
    queries = extract_sql_from_tool_calls(tool_calls)
    if not queries:
        return AssertionResult(
            name="sql_valid",
            passed=True,
            detail="No run_sql calls (use tool_called to assert presence)",
        )
    for query in queries:
        select_check = sql_is_valid_select(query)
        if not select_check.passed:
            return AssertionResult(
                name="sql_valid",
                passed=False,
                detail=f"Not a SELECT: {query[:80]}",
            )
        columns_check = sql_uses_valid_columns(query)
        if not columns_check.passed:
            return AssertionResult(
                name="sql_valid",
                passed=False,
                detail=columns_check.detail,
            )
    return AssertionResult(
        name="sql_valid",
        passed=True,
        detail=f"{len(queries)} query(ies) valid",
    )


# ---------------------------------------------------------------------------
# Chart assertions
# ---------------------------------------------------------------------------

# Year range considered valid for x-axis dates in chart code.
_VALID_YEAR_RANGE = range(2024, 2030)


def _assert_chart_renders(
    response: str,
    tool_calls: list | None,
    health_data: dict,
    db_seed: dict | None,
    query_rows: list[dict],
) -> AssertionResult:
    """Assert that all chart blocks render successfully.

    Args:
        response: Final model response text.
        tool_calls: Tool calls emitted during execution.
        health_data: Prompt-visible health data for the case.
        db_seed: Optional extended DB seed used for SQL/chart assertions.
        query_rows: Query rows accumulated during the tool loop.

    Returns:
        Assertion outcome.
    """
    from charts import extract_charts, render_chart

    blocks = extract_charts(response)
    if not blocks:
        return AssertionResult(
            name="chart_renders",
            passed=False,
            detail="No chart blocks found",
        )

    rows = list(query_rows)
    if not rows:
        queries = extract_sql_from_tool_calls(tool_calls)
        if queries:
            conn = build_eval_db(health_data, db_seed=db_seed)
            try:
                for query in queries:
                    query_result = execute_sql(conn, query)
                    if query_result and "_error" not in query_result[0]:
                        rows.extend(query_result)
            finally:
                conn.close()

    for block in blocks:
        extra_ns = {"rows": rows} if rows else {"rows": []}
        img = render_chart(block.code, {}, extra_namespace=extra_ns)
        if img is None:
            return AssertionResult(
                name="chart_renders",
                passed=False,
                detail=f"Failed to render chart: {block.title}",
            )

        # Assert x-axis dates are sane (catch wrong-year bugs).
        year_matches = re.findall(r"\b((?:19|20)\d{2})\b", block.code)
        bad_years = [y for y in year_matches if int(y) not in _VALID_YEAR_RANGE]
        if bad_years:
            return AssertionResult(
                name="chart_renders",
                passed=False,
                detail=f"Suspicious year(s) in chart code: {bad_years}",
            )

    return AssertionResult(
        name="chart_renders",
        passed=True,
        detail=f"{len(blocks)} chart(s) rendered",
    )


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
    table.add_column("Suite")
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
            r.suite,
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
    cache_hits = sum(1 for r in results if r.cached)
    summary = f"\n{passed}/{total} passed"
    if cache_hits:
        summary += f"  |  cache: {cache_hits}/{total} hit"
    if total_cost:
        summary += f"  |  total cost: ${total_cost:.4f}"
    console.print(summary)
    suites = sorted({r.suite for r in results})
    if len(suites) > 1:
        suite_bits = []
        for suite in suites:
            suite_results = [r for r in results if r.suite == suite]
            suite_passed = sum(1 for r in suite_results if r.passed)
            suite_bits.append(f"{suite}: {suite_passed}/{len(suite_results)}")
        console.print("Suite summary: " + " | ".join(suite_bits))

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
