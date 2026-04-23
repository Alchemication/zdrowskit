"""Small feedback-driven eval framework.

The framework is intentionally narrow: cases are curated from real thumbs-down
feedback, and each case runs through the current production prompt path. The
runner captures tool calls, but never writes context files.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import llm  # noqa: E402
import llm_context  # noqa: E402
import llm_health  # noqa: E402
from charts import strip_charts  # noqa: E402
from config import PROMPTS_DIR  # noqa: E402
from context_edit import context_edit_from_tool_call  # noqa: E402
from tools import all_chat_tools  # noqa: E402

CASES_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_MODEL = llm.DEFAULT_MODEL
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / ".cache.sqlite"
EVAL_CACHE_SCHEMA_VERSION = 2
EVAL_TEMPERATURE = 0.0


@dataclass(frozen=True)
class EvalCase:
    """A curated eval case derived from one real feedback item."""

    id: str
    feature: str
    case_kind: str
    source_feedback_id: int
    source_llm_call_id: int
    derived_from: dict[str, Any]
    intent: str
    fixture: dict[str, Any]
    assertions: list[dict[str, Any]]
    notes: str = ""


@dataclass(frozen=True)
class CapturedToolCall:
    """A parsed tool call emitted during an eval run."""

    name: str
    arguments: dict[str, Any]
    tool_call_id: str


@dataclass(frozen=True)
class AssertionResult:
    """Result of one deterministic assertion."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalExecution:
    """Captured output from a case run before assertions."""

    text: str
    tool_calls: list[CapturedToolCall] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_s: float = 0.0
    cost: float | None = None
    cache_hits: int = 0
    cache_misses: int = 0


@dataclass
class EvalResult:
    """Final eval outcome for one case and model."""

    case_id: str
    feature: str
    case_kind: str
    model: str
    source_feedback_id: int
    source_llm_call_id: int
    assertions: list[AssertionResult] = field(default_factory=list)
    execution: EvalExecution | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        """Whether every assertion passed and no runner error occurred."""
        return self.error is None and all(
            assertion.passed for assertion in self.assertions
        )

    @property
    def failures(self) -> list[AssertionResult]:
        """Failed assertion results."""
        return [assertion for assertion in self.assertions if not assertion.passed]


class EvalCache:
    """SQLite-backed cache for eval-time LLM responses."""

    def __init__(self, path: Path = DEFAULT_CACHE_PATH) -> None:
        """Create a cache handle for the given SQLite file."""
        self.path = path
        self._ensure_schema()

    def get(self, request: dict[str, Any]) -> llm.LLMResult | None:
        """Return a cached LLM result for the normalized request payload."""
        key = self._request_key(request)
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT response_json FROM llm_eval_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None

        cached = json.loads(str(row[0]))
        raw_message = cached.get("raw_message")
        tool_calls = None
        if isinstance(raw_message, dict):
            tool_calls = list(raw_message.get("tool_calls", []) or []) or None
        return llm.LLMResult(
            text=str(cached.get("text", "")),
            model=str(cached.get("model", request["model"])),
            input_tokens=int(cached.get("input_tokens", 0)),
            output_tokens=int(cached.get("output_tokens", 0)),
            total_tokens=int(cached.get("total_tokens", 0)),
            latency_s=float(cached.get("latency_s", 0.0) or 0.0),
            cost=(
                float(cached["cost"]) if cached.get("cost", None) is not None else None
            ),
            tool_calls=tool_calls,
            raw_message=raw_message if isinstance(raw_message, dict) else None,
        )

    def put(self, request: dict[str, Any], result: llm.LLMResult) -> None:
        """Persist an LLM result for the normalized request payload."""
        key = self._request_key(request)
        request_json = json.dumps(request, sort_keys=True)
        response_json = json.dumps(
            {
                "text": getattr(result, "text", ""),
                "model": getattr(result, "model", request["model"]),
                "input_tokens": getattr(result, "input_tokens", 0),
                "output_tokens": getattr(result, "output_tokens", 0),
                "total_tokens": getattr(result, "total_tokens", 0),
                "latency_s": getattr(result, "latency_s", 0.0),
                "cost": getattr(result, "cost", None),
                "raw_message": getattr(result, "raw_message", None),
            },
            sort_keys=True,
        )
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO llm_eval_cache (cache_key, request_json, response_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    request_json = excluded.request_json,
                    response_json = excluded.response_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (key, request_json, response_json),
            )
            conn.commit()

    def _ensure_schema(self) -> None:
        """Create the cache table on first use."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_eval_cache (
                    cache_key TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    @staticmethod
    def _request_key(request: dict[str, Any]) -> str:
        """Build a stable cache key for a normalized request payload."""
        encoded = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def load_cases(cases_dir: Path = CASES_DIR) -> list[EvalCase]:
    """Load all curated feedback eval cases.

    Args:
        cases_dir: Directory containing one JSON file per case.

    Returns:
        Cases sorted by id.
    """
    cases: list[EvalCase] = []
    for path in sorted(cases_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(_case_from_dict(raw, path))
    if not cases:
        raise ValueError(f"No eval cases found in {cases_dir}")
    return sorted(cases, key=lambda case: case.id)


def run_case(
    case: EvalCase,
    *,
    model: str = DEFAULT_MODEL,
    max_tool_iterations: int = 5,
    reasoning_effort: str | None = None,
    temperature: float | None = EVAL_TEMPERATURE,
    cache: EvalCache | None = None,
    refresh_cache: bool = False,
) -> EvalResult:
    """Run one eval case and evaluate its deterministic assertions.

    Pass ``temperature=None`` for models that reject the parameter entirely
    (e.g. claude-opus-4-7).
    """
    result = EvalResult(
        case_id=case.id,
        feature=case.feature,
        case_kind=case.case_kind,
        model=model,
        source_feedback_id=case.source_feedback_id,
        source_llm_call_id=case.source_llm_call_id,
    )
    try:
        if case.feature != "chat":
            raise ValueError(f"Unsupported eval feature: {case.feature}")
        execution = _run_chat_case(
            case,
            model=model,
            max_tool_iterations=max_tool_iterations,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )
        result.execution = execution
        result.assertions = run_assertions(case.assertions, execution)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def run_assertions(
    assertions: list[dict[str, Any]],
    execution: EvalExecution,
) -> list[AssertionResult]:
    """Evaluate deterministic assertions against a captured execution."""
    return [_evaluate_assertion(assertion, execution) for assertion in assertions]


def print_results(results: list[EvalResult]) -> None:
    """Print a compact human-readable result table."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            execution = result.execution
            print(
                f"{status} {result.case_id} "
                f"latency={_format_latency(execution)} cost={_format_cost(execution)}"
            )
        print(_format_pass_fail_summary(results))
        failed_summary = _format_failed_case_summary(results)
        if failed_summary is not None:
            print(failed_summary)
        if len(results) > 1:
            print(_format_summary_metrics(results))
        return

    console = Console(highlight=False)
    table = Table(title="Feedback Eval Results", show_lines=False)
    table.add_column("Case", style="bold")
    table.add_column("Feature")
    table.add_column("Kind")
    table.add_column("Model", style="dim")
    table.add_column("Source")
    table.add_column("Latency", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Pass", justify="center")
    table.add_column("Failures")

    for result in results:
        execution = result.execution
        if result.error:
            failures = result.error
        else:
            failures = "; ".join(
                f"{failure.name}: {failure.detail}" if failure.detail else failure.name
                for failure in result.failures
            )
        table.add_row(
            result.case_id,
            result.feature,
            result.case_kind,
            result.model.split("/")[-1],
            f"fb#{result.source_feedback_id}/call#{result.source_llm_call_id}",
            _format_latency(execution),
            _format_cost(execution),
            "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]",
            failures or "-",
        )
    console.print(table)
    summary_table = Table(title="Run Summary", show_header=False, box=None)
    summary_table.add_column("Metric", style="dim", no_wrap=True)
    summary_table.add_column("Value")
    for label, value in _summary_rows(results, text_cls=Text):
        summary_table.add_row(label, value)
    console.print(summary_table)


def print_result_details(results: list[EvalResult]) -> None:
    """Print captured response/tool details for debugging failed evals."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.syntax import Syntax
    except ImportError:
        for result in results:
            execution = result.execution
            print(f"\n{result.case_id}")
            print("error:", result.error or "-")
            print("text:", execution.text if execution else "(no execution)")
            print("tools:", execution.tool_calls if execution else [])
        return

    console = Console(highlight=False)
    for result in results:
        execution = result.execution
        if result.passed and not result.error:
            continue
        parts = [f"Error: {result.error or '-'}"]
        if execution is not None:
            parts.append(
                f"Latency: {_format_latency(execution)} | Cost: {_format_cost(execution)}"
            )
            parts.append(f"Final text:\n{execution.text or '(empty)'}")
            tools = [
                {"name": call.name, "arguments": call.arguments}
                for call in execution.tool_calls
            ]
            parts.append("Captured tools:")
            parts.append(json.dumps(tools, indent=2))
        console.print(
            Panel(
                "\n\n".join(parts),
                title=f"Details: {result.case_id}",
                border_style="yellow",
            )
        )
        if execution is not None and execution.tool_calls:
            console.print(
                Syntax(
                    json.dumps(
                        [
                            {"name": call.name, "arguments": call.arguments}
                            for call in execution.tool_calls
                        ],
                        indent=2,
                    ),
                    "json",
                    theme="ansi_dark",
                )
            )


def _format_latency(execution: EvalExecution | None) -> str:
    """Format eval latency for compact display."""
    if execution is None:
        return "-"
    return f"{execution.latency_s:.2f}s"


def _format_cost(execution: EvalExecution | None) -> str:
    """Format eval cost for compact display."""
    if execution is None or execution.cost is None:
        return "-"
    return f"${execution.cost:.4f}"


def _format_summary_metrics(results: list[EvalResult]) -> str:
    """Build a compact aggregate metrics summary for multi-case runs."""
    latencies = [
        result.execution.latency_s for result in results if result.execution is not None
    ]
    costs = [
        result.execution.cost
        for result in results
        if result.execution is not None and result.execution.cost is not None
    ]
    cache_hits = sum(
        result.execution.cache_hits
        for result in results
        if result.execution is not None
    )
    cache_misses = sum(
        result.execution.cache_misses
        for result in results
        if result.execution is not None
    )
    parts: list[str] = []
    if latencies:
        total_latency = sum(latencies)
        avg_latency = total_latency / len(latencies)
        p95_latency = _percentile_nearest_rank(latencies, 0.95)
        parts.append(f"latency total {total_latency:.2f}s")
        parts.append(f"avg {avg_latency:.2f}s")
        parts.append(f"p95 {p95_latency:.2f}s")
    if costs:
        parts.append(f"estimated cost ${sum(costs):.4f}")
    if cache_hits or cache_misses:
        parts.append(f"cache hits {cache_hits}")
        parts.append(f"misses {cache_misses}")
    if not parts:
        return "LLM summary: no execution metrics captured"
    return "LLM summary: " + " | ".join(parts)


def _format_pass_fail_summary(results: list[EvalResult]) -> str:
    """Build a compact pass/fail summary for the result footer."""
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    accuracy = (passed / len(results) * 100.0) if results else 0.0
    return f"Accuracy: {accuracy:.1f}% | Passed: {passed} | Failed: {failed}"


def _format_failed_case_summary(results: list[EvalResult]) -> str | None:
    """Build a compact failed-case list for the result footer."""
    failed_case_ids = [result.case_id for result in results if not result.passed]
    if not failed_case_ids:
        return None
    return "Failed cases: " + ", ".join(failed_case_ids)


def _summary_rows(
    results: list[EvalResult],
    *,
    text_cls: type | None = None,
) -> list[tuple[str, Any]]:
    """Build rich-summary rows for the eval footer."""
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    accuracy = (passed / len(results) * 100.0) if results else 0.0
    rows: list[tuple[str, Any]] = [
        ("Accuracy", _render_accuracy_value(accuracy, text_cls=text_cls)),
        ("Passed", str(passed)),
        ("Failed", str(failed)),
    ]
    failed_summary = _format_failed_case_summary(results)
    if failed_summary is not None:
        rows.append(("Failed Cases", failed_summary.removeprefix("Failed cases: ")))
    if len(results) <= 1:
        return rows

    latencies = [
        result.execution.latency_s for result in results if result.execution is not None
    ]
    costs = [
        result.execution.cost
        for result in results
        if result.execution is not None and result.execution.cost is not None
    ]
    cache_hits = sum(
        result.execution.cache_hits
        for result in results
        if result.execution is not None
    )
    cache_misses = sum(
        result.execution.cache_misses
        for result in results
        if result.execution is not None
    )
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        p95_latency = _percentile_nearest_rank(latencies, 0.95)
        rows.extend(
            [
                ("Latency Avg", f"{avg_latency:.2f}s"),
                ("Latency p95", f"{p95_latency:.2f}s"),
            ]
        )
    if costs:
        total_cost = sum(costs)
        avg_cost = total_cost / len(costs)
        rows.extend(
            [
                ("Estimated Cost", f"${total_cost:.4f}"),
                ("Avg Cost", f"${avg_cost:.4f}"),
            ]
        )
    if cache_hits or cache_misses:
        rows.append(("Cache", f"{cache_hits} hits, {cache_misses} misses"))
    return rows


def _render_accuracy_value(
    accuracy: float,
    *,
    text_cls: type | None = None,
) -> Any:
    """Render the accuracy value with a threshold-based color when available."""
    label = f"{accuracy:.1f}%"
    if text_cls is None:
        return label
    if accuracy >= 80.0:
        style = "green"
    elif accuracy >= 50.0:
        style = "yellow"
    else:
        style = "red"
    return text_cls(label, style=style)


def _percentile_nearest_rank(values: list[float], percentile: float) -> float:
    """Return the nearest-rank percentile for a non-empty numeric list."""
    sorted_values = sorted(values)
    rank = max(1, math.ceil(percentile * len(sorted_values)))
    return sorted_values[rank - 1]


def _case_from_dict(raw: dict[str, Any], path: Path) -> EvalCase:
    required = {
        "id",
        "feature",
        "case_kind",
        "source_feedback_id",
        "source_llm_call_id",
        "derived_from",
        "intent",
        "fixture",
        "assertions",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{path} missing required fields: {missing}")
    if not isinstance(raw["assertions"], list) or not raw["assertions"]:
        raise ValueError(f"{path} must define at least one assertion")
    fixture = raw["fixture"]
    if not isinstance(fixture, dict):
        raise ValueError(f"{path} fixture must be an object")
    if "today" not in fixture or "context" not in fixture or "turns" not in fixture:
        raise ValueError(f"{path} fixture must include today, context, and turns")
    return EvalCase(
        id=str(raw["id"]),
        feature=str(raw["feature"]),
        case_kind=str(raw["case_kind"]),
        source_feedback_id=int(raw["source_feedback_id"]),
        source_llm_call_id=int(raw["source_llm_call_id"]),
        derived_from=dict(raw["derived_from"]),
        intent=str(raw["intent"]),
        fixture=fixture,
        assertions=raw["assertions"],
        notes=str(raw.get("notes", "")),
    )


def _run_chat_case(
    case: EvalCase,
    *,
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: EvalCache | None,
    refresh_cache: bool,
) -> EvalExecution:
    fixture = case.fixture
    today = date.fromisoformat(str(fixture["today"]))
    context = _build_context(fixture)
    health_data_text = llm_health.render_health_data(
        fixture.get("health_data", {}),
        prompt_kind="chat",
        today=today,
    )
    messages: list[dict[str, Any]] = llm_context.build_messages(
        context,
        health_data_text=health_data_text,
        baselines=fixture.get("baselines"),
        today=today,
    )
    messages.extend(_fixture_turns(fixture))

    tools = all_chat_tools()
    captured: list[CapturedToolCall] = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    latency_s = 0.0
    cost = 0.0
    cache_hits = 0
    cache_misses = 0

    last_result: Any = None
    for iteration in range(max_tool_iterations):
        last_result, cache_hit = _call_llm_for_eval(
            messages=messages,
            model=model,
            max_tokens=int(fixture.get("max_tokens", 1024)),
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=tools,
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                "iteration": iteration,
            },
            cache=cache,
            refresh_cache=refresh_cache,
        )
        if cache_hit:
            cache_hits += 1
        else:
            cache_misses += 1
        input_tokens += int(getattr(last_result, "input_tokens", 0) or 0)
        output_tokens += int(getattr(last_result, "output_tokens", 0) or 0)
        total_tokens += int(getattr(last_result, "total_tokens", 0) or 0)
        latency_s += float(getattr(last_result, "latency_s", 0.0) or 0.0)
        if getattr(last_result, "cost", None) is not None:
            cost += float(last_result.cost)

        tool_calls = _result_tool_calls(last_result)
        if not tool_calls:
            return EvalExecution(
                text=str(getattr(last_result, "text", "") or ""),
                tool_calls=captured,
                messages=messages,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_s=latency_s,
                cost=cost or None,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
            )

        messages.append(_assistant_message(last_result))
        for raw_tool_call in tool_calls:
            tool_call = _capture_tool_call(raw_tool_call)
            captured.append(tool_call)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.tool_call_id,
                    "content": _eval_tool_result(tool_call, fixture),
                }
            )

    if last_result is not None and _result_tool_calls(last_result):
        last_result, cache_hit = _call_llm_for_eval(
            messages=messages,
            model=model,
            max_tokens=int(fixture.get("max_tokens", 1024)),
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=None,
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                "iteration": "final_synthesis",
            },
            cache=cache,
            refresh_cache=refresh_cache,
        )
        if cache_hit:
            cache_hits += 1
        else:
            cache_misses += 1
        input_tokens += int(getattr(last_result, "input_tokens", 0) or 0)
        output_tokens += int(getattr(last_result, "output_tokens", 0) or 0)
        total_tokens += int(getattr(last_result, "total_tokens", 0) or 0)
        latency_s += float(getattr(last_result, "latency_s", 0.0) or 0.0)
        if getattr(last_result, "cost", None) is not None:
            cost += float(last_result.cost)

    return EvalExecution(
        text=str(getattr(last_result, "text", "") if last_result is not None else ""),
        tool_calls=captured,
        messages=messages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        latency_s=latency_s,
        cost=cost or None,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )


def _call_llm_for_eval(
    *,
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    reasoning_effort: str | None,
    temperature: float | None,
    tools: list[dict[str, Any]] | None,
    metadata: dict[str, Any],
    cache: EvalCache | None,
    refresh_cache: bool,
) -> tuple[llm.LLMResult, bool]:
    """Call the LLM for an eval case with optional request caching."""
    request = {
        "cache_schema_version": EVAL_CACHE_SCHEMA_VERSION,
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
        "tools": tools,
    }
    if cache is not None and not refresh_cache:
        cached = cache.get(request)
        if cached is not None:
            return cached, True

    result = llm.call_llm(
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        tools=tools,
        request_type="",
        metadata=metadata,
    )
    if cache is not None:
        cache.put(request, result)
    return result, False


def _build_context(fixture: dict[str, Any]) -> dict[str, str]:
    context = {key: str(value) for key, value in fixture["context"].items()}
    context["prompt"] = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
    soul_path = PROMPTS_DIR / "soul.md"
    context["soul"] = (
        soul_path.read_text(encoding="utf-8")
        if soul_path.exists()
        else llm_context.DEFAULT_SOUL
    )
    return context


def _fixture_turns(fixture: dict[str, Any]) -> list[dict[str, str]]:
    turns = fixture.get("turns", [])
    if not isinstance(turns, list) or not turns:
        raise ValueError("fixture.turns must be a non-empty list")
    cleaned: list[dict[str, str]] = []
    for turn in turns:
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise ValueError(f"Invalid fixture turn: {turn}")
        cleaned.append({"role": role, "content": content})
    return cleaned


def _assistant_message(result: Any) -> dict[str, Any]:
    raw = getattr(result, "raw_message", None)
    if isinstance(raw, dict):
        return raw
    message: dict[str, Any] = {
        "role": "assistant",
        "content": str(getattr(result, "text", "") or ""),
    }
    tool_calls = _result_tool_calls(result)
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": _tool_call_id(tool_call),
                "type": "function",
                "function": {
                    "name": _tool_name(tool_call),
                    "arguments": json.dumps(_tool_arguments(tool_call)),
                },
            }
            for tool_call in tool_calls
        ]
    return message


def _result_tool_calls(result: Any) -> list[Any]:
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    if tool_calls:
        return tool_calls
    raw = getattr(result, "raw_message", None)
    if isinstance(raw, dict):
        return list(raw.get("tool_calls", []) or [])
    return []


def _capture_tool_call(raw_tool_call: Any) -> CapturedToolCall:
    name = _tool_name(raw_tool_call)
    arguments = _tool_arguments(raw_tool_call)
    if name == "update_context" and arguments.get("action") in {
        "append",
        "replace_section",
    }:
        normalized_edit = context_edit_from_tool_call(
            _tool_call_namespace(raw_tool_call)
        )
        if normalized_edit is not None:
            arguments = {
                "file": normalized_edit.file,
                "action": normalized_edit.action,
                "content": normalized_edit.content,
                "summary": normalized_edit.summary,
            }
            if normalized_edit.section is not None:
                arguments["section"] = normalized_edit.section
    return CapturedToolCall(
        name=name,
        arguments=arguments,
        tool_call_id=_tool_call_id(raw_tool_call),
    )


def _tool_name(raw_tool_call: Any) -> str:
    function = _tool_function(raw_tool_call)
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(getattr(function, "name", ""))


def _tool_arguments(raw_tool_call: Any) -> dict[str, Any]:
    function = _tool_function(raw_tool_call)
    raw_args = (
        function.get("arguments", "{}")
        if isinstance(function, dict)
        else getattr(function, "arguments", "{}")
    )
    if isinstance(raw_args, dict):
        return raw_args
    try:
        parsed = json.loads(str(raw_args or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_call_id(raw_tool_call: Any) -> str:
    if isinstance(raw_tool_call, dict):
        return str(raw_tool_call.get("id", "call_unknown"))
    return str(getattr(raw_tool_call, "id", "call_unknown"))


def _tool_function(raw_tool_call: Any) -> Any:
    if isinstance(raw_tool_call, dict):
        return raw_tool_call.get("function", {})
    return getattr(raw_tool_call, "function", {})


def _tool_call_namespace(raw_tool_call: Any) -> Any:
    """Convert a raw tool-call payload into an attribute-access object."""
    if not isinstance(raw_tool_call, dict):
        return raw_tool_call
    function = raw_tool_call.get("function", {})
    return SimpleNamespace(
        id=str(raw_tool_call.get("id", "call_unknown")),
        function=SimpleNamespace(
            name=str(function.get("name", "")),
            arguments=function.get("arguments", "{}"),
        ),
    )


def _eval_tool_result(tool_call: CapturedToolCall, fixture: dict[str, Any]) -> str:
    if tool_call.name == "update_context":
        return "Proposed. User will be asked to confirm."
    if tool_call.name == "run_sql":
        return _execute_seed_sql(tool_call.arguments, fixture.get("db_seed"))
    return json.dumps({"error": f"Unknown tool: {tool_call.name}"})


def _execute_seed_sql(arguments: dict[str, Any], db_seed: Any) -> str:
    if not db_seed:
        return json.dumps({"error": "This eval fixture does not define db_seed."})
    query = str(arguments.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "Empty query."})
    if query.lstrip("( \t\n").split()[0].upper() != "SELECT":
        return json.dumps({"error": "Only SELECT queries are allowed."})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _load_seed_tables(conn, db_seed)
        rows = [dict(row) for row in conn.execute(query).fetchall()]
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()
    return json.dumps(rows, default=str)


def _load_seed_tables(conn: sqlite3.Connection, db_seed: dict[str, Any]) -> None:
    tables = db_seed.get("tables", {})
    if not isinstance(tables, dict):
        raise ValueError("db_seed.tables must be an object")
    for table, rows in tables.items():
        if not isinstance(rows, list) or not rows:
            continue
        columns = sorted({key for row in rows if isinstance(row, dict) for key in row})
        if not columns:
            continue
        col_defs = ", ".join(f"{column} TEXT" for column in columns)
        conn.execute(f"CREATE TABLE {table} ({col_defs})")
        placeholders = ", ".join(["?"] * len(columns))
        col_names = ", ".join(columns)
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                [row.get(column) for column in columns],
            )
    conn.commit()


def _evaluate_assertion(
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    atype = assertion.get("type")
    name = str(assertion.get("name", atype))
    if atype == "tool_called":
        return _assert_tool_called(name, assertion, execution)
    if atype == "tool_not_called":
        return _assert_tool_not_called(name, assertion, execution)
    if atype == "tool_arg_matches":
        return _assert_tool_arg_matches(name, assertion, execution)
    if atype == "text_contains":
        return _assert_text_contains(name, assertion, execution)
    if atype == "text_absent":
        return _assert_text_absent(name, assertion, execution)
    if atype == "text_without_chart_absent":
        return _assert_text_without_chart_absent(name, assertion, execution)
    if atype == "word_count_max":
        return _assert_word_count_max(name, assertion, execution)
    if atype == "forbidden_opening":
        return _assert_forbidden_opening(name, assertion, execution)
    return AssertionResult(
        name=name, passed=False, detail=f"Unknown assertion type: {atype}"
    )


def _assert_tool_called(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    tool = str(assertion["tool"])
    calls = [call for call in execution.tool_calls if call.name == tool]
    expected = assertion.get("count")
    if expected is not None:
        passed = len(calls) == int(expected)
        detail = f"{tool}: {len(calls)} call(s), expected {expected}"
    else:
        min_count = int(assertion.get("min_count", 1))
        max_count = assertion.get("max_count")
        passed = len(calls) >= min_count and (
            max_count is None or len(calls) <= int(max_count)
        )
        detail = f"{tool}: {len(calls)} call(s)"
    return AssertionResult(name=name, passed=passed, detail="" if passed else detail)


def _assert_tool_not_called(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    tool = str(assertion["tool"])
    calls = [call for call in execution.tool_calls if call.name == tool]
    return AssertionResult(
        name=name,
        passed=not calls,
        detail="" if not calls else f"{tool}: {len(calls)} unexpected call(s)",
    )


def _assert_tool_arg_matches(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    tool = str(assertion["tool"])
    matches = assertion.get("matches", {})
    calls = [call for call in execution.tool_calls if call.name == tool]
    for call in calls:
        if all(
            _value_matches(call.arguments.get(key), expected)
            for key, expected in matches.items()
        ):
            return AssertionResult(name=name, passed=True)
    return AssertionResult(
        name=name,
        passed=False,
        detail=f"No {tool} call matched {matches}; got {[call.arguments for call in calls]}",
    )


def _assert_text_contains(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    missing = [
        pattern
        for pattern in assertion.get("patterns", [])
        if not _text_matches(execution.text, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not missing,
        detail="" if not missing else f"Missing: {missing}",
    )


def _assert_text_absent(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    present = [
        pattern
        for pattern in assertion.get("patterns", [])
        if _text_matches(execution.text, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not present,
        detail="" if not present else f"Present: {present}",
    )


def _assert_word_count_max(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    max_words = int(assertion["max_words"])
    count = len(re.findall(r"\S+", execution.text.strip()))
    return AssertionResult(
        name=name,
        passed=count <= max_words,
        detail=f"{count} words, max {max_words}",
    )


def _assert_text_without_chart_absent(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    visible_text = strip_charts(execution.text)
    present = [
        pattern
        for pattern in assertion.get("patterns", [])
        if _text_matches(visible_text, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not present,
        detail="" if not present else f"Present after chart stripping: {present}",
    )


def _assert_forbidden_opening(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    text = execution.text.lstrip()
    for pattern in assertion.get("patterns", []):
        pattern_text = str(pattern)
        if text.lower().startswith(pattern_text.lower()):
            return AssertionResult(
                name=name,
                passed=False,
                detail=f"Started with forbidden opening: {pattern_text}",
            )
    return AssertionResult(name=name, passed=True)


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        text = str(actual or "")
        if "equals" in expected and actual != expected["equals"]:
            return False
        contains = expected.get("contains", [])
        if contains and any(str(item).lower() not in text.lower() for item in contains):
            return False
        regex = expected.get("regex")
        if regex and re.search(str(regex), text, re.IGNORECASE) is None:
            return False
        return True
    if isinstance(expected, str) and expected.startswith("re:"):
        return re.search(expected[3:], str(actual or ""), re.IGNORECASE) is not None
    return actual == expected


def _text_matches(text: str, pattern: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.IGNORECASE) is not None
    return pattern.lower() in text.lower()
