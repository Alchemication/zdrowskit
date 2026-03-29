"""Eval framework — base class, blueprint loading, assertion helpers, results display.

Building blocks:
    load_blueprints()   — read pinned context + health data from committed snapshots
    build_eval_messages() — thin wrapper around build_messages() with pinned date
    Eval                — base class; subclasses define scenarios + assertions
    AssertionResult     — single assertion outcome
    EvalResult          — aggregated outcome for one eval × scenario × model
    print_results()     — rich table output

Assertion helpers:
    has_sections         — check for required ## headings
    word_count_under     — visible text word count
    pace_format_valid    — mm:ss/km, not decimal
    has_memory_block     — <memory> block present
    no_markdown_tables   — no pipe-delimited tables (Telegram compat)
    response_is_skip     — response is exactly SKIP
    response_is_not_skip — response is not SKIP
    chart_code_executes  — chart code renders to valid PNG
    sql_is_valid_select  — SQL starts with SELECT
    sql_uses_valid_columns — SQL uses known schema identifiers
"""

from __future__ import annotations

import json
import re
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable
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

DEFAULT_MODEL = "anthropic/claude-opus-4-6"


# ---------------------------------------------------------------------------
# Blueprint loading
# ---------------------------------------------------------------------------

_CONTEXT_STEMS = ["me", "goals", "plan", "log", "history", "baselines"]


def load_blueprints(
    prompt_file: str = "prompt",
) -> tuple[dict[str, str], dict, dict]:
    """Load pinned context, health data, and metadata from committed blueprints.

    Prompt templates (soul.md, prompt.md, etc.) are loaded live from
    ``src/prompts/`` — they are version-controlled code, not user data.
    User context files come from the blueprint snapshot.

    Args:
        prompt_file: Which prompt template to load (e.g. "prompt",
            "nudge_prompt", "chat_prompt").

    Returns:
        (context, health_data, metadata) tuple.
    """
    from config import PROMPTS_DIR

    context: dict[str, str] = {}

    # Load prompt template from src/prompts/.
    prompt_path = PROMPTS_DIR / f"{prompt_file}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt template missing: {prompt_path}")
    context["prompt"] = prompt_path.read_text(encoding="utf-8")

    soul_path = PROMPTS_DIR / "soul.md"
    if soul_path.exists():
        context["soul"] = soul_path.read_text(encoding="utf-8")

    # Load user context files from blueprints.
    ctx_dir = _BLUEPRINTS_DIR / "context"
    for stem in _CONTEXT_STEMS:
        path = ctx_dir / f"{stem}.md"
        if path.exists():
            context[stem] = path.read_text(encoding="utf-8")
        else:
            context[stem] = "(not provided)"

    # Health data.
    hd_path = _BLUEPRINTS_DIR / "health_data.json"
    health_data = json.loads(hd_path.read_text(encoding="utf-8"))

    # Metadata.
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
    """Build LLM messages using pinned date from metadata.

    Mirrors ``build_messages()`` from ``src/llm.py`` but uses the
    extraction date instead of ``date.today()`` for reproducibility.
    """
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
    """Aggregated outcome of one eval × scenario × model."""

    eval_name: str
    scenario: str
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
# Base class
# ---------------------------------------------------------------------------


class Eval(ABC):
    """Base class for an eval case.

    Subclasses define which scenarios to run and what to assert.
    The ``run()`` method handles the full pipeline: load blueprints,
    apply scenario, build messages, call LLM, run assertions.
    """

    name: str = ""

    @abstractmethod
    def eval_scenarios(self) -> list[tuple[str, Callable, dict]]:
        """Return scenarios this eval uses.

        Each entry is ``(scenario_name, scenario_fn, extra_config)``.
        ``extra_config`` keys: ``prompt_file``, ``trigger_type``,
        ``recent_nudges``, ``tools``, ``max_tokens``, ``extra_messages``,
        ``week_complete``.
        """

    @abstractmethod
    def assertions(
        self,
        response: str,
        tool_calls: list | None = None,
        health_data: dict | None = None,
    ) -> list[AssertionResult]:
        """Run structural assertions on the LLM output."""

    def scenario_count(self, scenario_filter: str | None = None) -> int:
        """Number of scenarios that will run (for progress tracking)."""
        if scenario_filter:
            return sum(
                1 for name, _, _ in self.eval_scenarios() if name == scenario_filter
            )
        return len(self.eval_scenarios())

    def run(
        self,
        model: str,
        reasoning_effort: str | None = None,
        scenario_filter: str | None = None,
        progress: Any = None,
        task_id: Any = None,
    ) -> list[EvalResult]:
        """Run all (or filtered) scenarios against one model."""
        from llm import call_llm

        results: list[EvalResult] = []

        for scenario_name, scenario_fn, config in self.eval_scenarios():
            if scenario_filter and scenario_name != scenario_filter:
                continue

            if progress and task_id is not None:
                model_short = model.split("/")[-1] if "/" in model else model
                progress.update(
                    task_id,
                    description=f"[bold]{self.name}[/bold]/{scenario_name} on {model_short}",
                )

            prompt_file = config.get("prompt_file", "prompt")
            context, health_data, metadata = load_blueprints(prompt_file)
            ctx, hd = scenario_fn(deepcopy(context), deepcopy(health_data))

            # Apply extra context keys (trigger_type, recent_nudges, etc.).
            for key in ("trigger_type", "recent_nudges"):
                if key in config:
                    ctx[key] = config[key]

            messages = build_eval_messages(
                ctx,
                hd,
                metadata,
                week_complete=config.get("week_complete"),
            )

            # Append extra messages (e.g. user question for chat).
            for msg in config.get("extra_messages", []):
                messages.append(msg)

            tools = config.get("tools")
            max_tokens = config.get("max_tokens", 4096)

            t0 = time.perf_counter()
            try:
                result = call_llm(
                    messages=messages,
                    model=model,
                    max_tokens=max_tokens,
                    tools=tools,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:
                results.append(
                    EvalResult(
                        eval_name=self.name,
                        scenario=scenario_name,
                        model=model,
                        error=str(exc),
                    )
                )
                if progress and task_id is not None:
                    progress.advance(task_id)
                continue
            latency = time.perf_counter() - t0

            checks = self.assertions(result.text, result.tool_calls, hd)
            results.append(
                EvalResult(
                    eval_name=self.name,
                    scenario=scenario_name,
                    model=model,
                    assertions=checks,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_s=latency,
                    cost=result.cost,
                )
            )
            if progress and task_id is not None:
                progress.advance(task_id)

        return results


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def has_sections(text: str, headings: list[str]) -> AssertionResult:
    """Check that *text* contains all expected ``##`` headings."""
    missing = [h for h in headings if h.lower() not in text.lower()]
    if missing:
        return AssertionResult(
            name="has_sections",
            passed=False,
            detail=f"Missing: {missing}",
        )
    return AssertionResult(name="has_sections", passed=True)


def word_count_under(text: str, limit: int) -> AssertionResult:
    """Visible text (no chart/memory blocks) is under *limit* words."""
    clean = re.sub(r"<chart\s[^>]*>.*?</chart>", "", text, flags=re.DOTALL)
    clean = re.sub(r"<memory>.*?</memory>", "", clean, flags=re.DOTALL)
    count = len(clean.split())
    passed = count <= limit
    return AssertionResult(
        name="word_count",
        passed=passed,
        detail=f"{count} words (limit {limit})",
    )


def pace_format_valid(text: str) -> AssertionResult:
    """Paces use mm:ss/km, not decimal."""
    clean = re.sub(r"<chart\s[^>]*>.*?</chart>", "", text, flags=re.DOTALL)
    clean = re.sub(r"`[^`]+`", "", clean)
    decimal_paces = re.findall(r"\d+\.\d+\s*/km", clean)
    if decimal_paces:
        return AssertionResult(
            name="pace_format",
            passed=False,
            detail=f"Decimal paces: {decimal_paces[:3]}",
        )
    return AssertionResult(name="pace_format", passed=True)


def has_memory_block(text: str) -> AssertionResult:
    """Well-formed ``<memory>`` block present and non-empty."""
    match = re.search(r"<memory>(.*?)</memory>", text, re.DOTALL)
    if not match:
        return AssertionResult(
            name="has_memory", passed=False, detail="No <memory> block"
        )
    if not match.group(1).strip():
        return AssertionResult(
            name="has_memory", passed=False, detail="Empty <memory> block"
        )
    return AssertionResult(name="has_memory", passed=True)


def no_markdown_tables(text: str) -> AssertionResult:
    """No pipe-delimited markdown tables (Telegram can't render them)."""
    clean = re.sub(r"<chart\s[^>]*>.*?</chart>", "", text, flags=re.DOTALL)
    table_rows = re.findall(r"^\s*\|.+\|.+\|", clean, re.MULTILINE)
    if table_rows:
        return AssertionResult(
            name="no_tables",
            passed=False,
            detail=f"{len(table_rows)} table rows",
        )
    return AssertionResult(name="no_tables", passed=True)


def response_is_skip(text: str) -> AssertionResult:
    """Response is exactly SKIP."""
    if text.strip().upper() == "SKIP":
        return AssertionResult(name="is_skip", passed=True)
    return AssertionResult(
        name="is_skip",
        passed=False,
        detail=f"Expected SKIP, got: {text[:100]}",
    )


def response_is_not_skip(text: str) -> AssertionResult:
    """Response is NOT SKIP."""
    if text.strip().upper() == "SKIP":
        return AssertionResult(
            name="is_not_skip", passed=False, detail="Got unexpected SKIP"
        )
    return AssertionResult(name="is_not_skip", passed=True)


def chart_code_executes(
    code: str,
    health_data: dict,
    extra_namespace: dict | None = None,
) -> AssertionResult:
    """Chart code renders to a valid PNG via ``render_chart()``."""
    from charts import render_chart

    png = render_chart(code, health_data, extra_namespace)
    if png is None:
        return AssertionResult(
            name="chart_executes", passed=False, detail="render_chart returned None"
        )
    if len(png) < 1024:
        return AssertionResult(
            name="chart_executes",
            passed=False,
            detail=f"PNG too small: {len(png)} bytes",
        )
    return AssertionResult(
        name="chart_executes", passed=True, detail=f"{len(png)} bytes"
    )


def sql_is_valid_select(query: str) -> AssertionResult:
    """SQL starts with SELECT."""
    stripped = query.strip().lstrip("( \t\n")
    first = stripped.split()[0].upper() if stripped else ""
    if first != "SELECT":
        return AssertionResult(
            name="sql_select", passed=False, detail=f"First keyword: {first}"
        )
    return AssertionResult(name="sql_select", passed=True)


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

# Common SQL identifiers that are not table/column names.
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


def sql_uses_valid_columns(query: str) -> AssertionResult:
    """SQL references only known schema identifiers (heuristic)."""
    all_columns = set()
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


# ---------------------------------------------------------------------------
# Results display
# ---------------------------------------------------------------------------


def print_results(results: list[EvalResult]) -> None:
    """Print a rich table of eval results."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Eval Results", show_lines=True)
    table.add_column("Eval", style="bold")
    table.add_column("Scenario")
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

        # Shorten model name for display.
        model_short = r.model.split("/")[-1] if "/" in r.model else r.model

        table.add_row(
            r.eval_name,
            r.scenario,
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

    # Aggregate per model.
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

    # --- Pass rate ---
    lines.append("  Pass Rate\n", style="bold")
    for model, s in model_stats.items():
        rate = s["passed"] / s["total"] if s["total"] else 0
        filled = round(rate * BAR_WIDTH)
        color = "green" if rate == 1.0 else "yellow" if rate >= 0.5 else "red"
        lines.append(f"  {model:>{label_width}} ")
        lines.append("█" * filled, style=color)
        lines.append("░" * (BAR_WIDTH - filled), style="dim")
        lines.append(f" {s['passed']}/{s['total']} ({rate:.0%})\n")

    # --- Avg latency ---
    max_lat = max(
        (sum(s["latency"]) / len(s["latency"]) if s["latency"] else 0)
        for s in model_stats.values()
    )
    if max_lat > 0:
        lines.append("\n  Avg Latency\n", style="bold")
        for model, s in model_stats.items():
            avg = sum(s["latency"]) / len(s["latency"]) if s["latency"] else 0
            filled = round((avg / max_lat) * BAR_WIDTH) if max_lat else 0
            color = (
                "green"
                if avg <= max_lat * 0.5
                else "yellow"
                if avg <= max_lat * 0.8
                else "red"
            )
            lines.append(f"  {model:>{label_width}} ")
            lines.append("█" * filled, style=color)
            lines.append("░" * (BAR_WIDTH - filled), style="dim")
            lines.append(f" {avg:.1f}s\n")

    # --- Cost ---
    max_cost = max(s["cost"] for s in model_stats.values())
    if max_cost > 0:
        lines.append("\n  Total Cost\n", style="bold")
        for model, s in model_stats.items():
            filled = round((s["cost"] / max_cost) * BAR_WIDTH) if max_cost else 0
            color = (
                "green"
                if s["cost"] <= max_cost * 0.3
                else "yellow"
                if s["cost"] <= max_cost * 0.7
                else "red"
            )
            lines.append(f"  {model:>{label_width}} ")
            lines.append("█" * filled, style=color)
            lines.append("░" * (BAR_WIDTH - filled), style="dim")
            lines.append(f" ${s['cost']:.4f}\n")

    # --- Tokens ---
    max_tok = max(s["tokens_in"] + s["tokens_out"] for s in model_stats.values())
    if max_tok > 0:
        lines.append("\n  Tokens (in+out)\n", style="bold")
        for model, s in model_stats.items():
            tok = s["tokens_in"] + s["tokens_out"]
            filled = round((tok / max_tok) * BAR_WIDTH) if max_tok else 0
            lines.append(f"  {model:>{label_width}} ")
            lines.append("█" * filled, style="cyan")
            lines.append("░" * (BAR_WIDTH - filled), style="dim")
            lines.append(f" {tok:,}\n")

    console.print(
        Panel(lines, title="Model Comparison", border_style="dim", expand=False)
    )
