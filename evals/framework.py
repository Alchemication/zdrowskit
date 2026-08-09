"""Small feedback-driven eval framework.

The framework is intentionally narrow: cases are curated from real thumbs-down
feedback, and each case runs through the current production prompt path. The
runner captures tool calls, but never writes context files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field, ValidationError

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import llm  # noqa: E402
import llm_context  # noqa: E402
import llm_health  # noqa: E402
from charts import strip_charts  # noqa: E402
from config import EVAL_EXECUTION_ATTEMPTS, PROMPTS_DIR  # noqa: E402
from context_edit import context_edit_from_tool_call  # noqa: E402
from tools import all_chat_tools  # noqa: E402

logger = logging.getLogger(__name__)

CASES_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_MODEL = llm.DEFAULT_MODEL

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / ".cache.sqlite"
EVAL_CACHE_SCHEMA_VERSION = 5
EVAL_TEMPERATURE = 0.0
DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-6"
EVAL_JUDGE_MAX_TOKENS = 800
EVAL_JUDGE_TEMPERATURE = 0.0
PRODUCTION_EFFORT = "production"
"""Sentinel asking a case to inherit its route's reasoning effort.

Distinct from None, which means reasoning off: a feature production runs
with thinking enabled must not be silently evaluated without it.
"""

EVAL_MAX_CONCURRENCY = 12
"""Ceiling on parallel eval executions, whatever `--concurrency` asks for.

Eval calls are network-bound, so parallelism is nearly free in wall clock. The
cap exists for the providers, not for us: a full suite fans out across three of
them, and a burst large enough to trip a rate limit turns into retries and
errored cases, which cost more time than the parallelism saved.
"""


# Which model_prefs feature backs each eval feature. Evals exist to check the
# production path, so by default they must ask the model production would ask —
# running chat cases on the pro tier while chat actually routes to flash tests
# a configuration nobody ships.
EVAL_FEATURE_TO_PRODUCTION_FEATURE = {
    "chat": "chat",
    "nudge": "nudge",
    "insights": "insights",
    "memory": "memory",
    "verification_judge": "verification",
}


def production_route(feature: str) -> dict[str, Any]:
    """Return the live model_prefs route backing one eval feature.

    Args:
        feature: Eval feature name.

    Returns:
        ``call_llm`` kwargs — model, and reasoning effort or temperature when
        the profile pins them.

    Raises:
        ValueError: If the eval feature has no production counterpart.
    """
    from model_prefs import resolve_model_route

    mapped = EVAL_FEATURE_TO_PRODUCTION_FEATURE.get(feature)
    if mapped is None:
        raise ValueError(f"No production route mapped for eval feature: {feature}")
    return resolve_model_route(mapped, path=_operator_prefs_path()).call_kwargs()


def _operator_prefs_path() -> Path | None:
    """Return the operator profile's model_prefs path, or None if unavailable.

    Routing is per profile, so the bare config default points at a file that
    does not exist in a profile install. resolve_model_route then falls back to
    shipped defaults, and the harness reports "production" while measuring a
    configuration the operator may have tuned away from months ago.
    """
    try:
        from profiles import load_profiles, operator_profile

        return operator_profile(load_profiles()).model_prefs
    except Exception:  # noqa: BLE001 - any roster problem means "use defaults"
        logger.warning(
            "Could not resolve the operator profile; eval routes fall back to "
            "shipped defaults rather than your configured models.",
            exc_info=True,
        )
        return None


class JudgeAssertionResult(BaseModel):
    """Structured result for one semantic judge assertion."""

    name: str = Field(description="Assertion name copied exactly.")
    reason: str = Field(description="Brief explanation of the judgment.")
    evidence: str = Field(
        description="Short quote or paraphrase from the candidate response."
    )
    passed: bool = Field(
        description="True only when the assertion is clearly satisfied."
    )


class JudgeResponse(BaseModel):
    """Structured response for a semantic eval judge call."""

    results: list[JudgeAssertionResult] = Field(
        description="One result for every supplied assertion, with no extras."
    )


@dataclass(frozen=True)
class EvalCase:
    """A curated eval case derived from one real feedback item."""

    id: str
    feature: str
    case_kind: str
    source_feedback_id: int | None
    source_llm_call_id: int
    derived_from: dict[str, Any]
    intent: str
    fixture: dict[str, Any]
    assertions: list[dict[str, Any]]
    judge_assertions: list[dict[str, str]] = field(default_factory=list)
    notes: str = ""


@dataclass(frozen=True)
class CapturedToolCall:
    """A parsed tool call emitted during an eval run."""

    name: str
    arguments: dict[str, Any]
    tool_call_id: str


@dataclass(frozen=True)
class AssertionResult:
    """Result of one deterministic or judge assertion."""

    name: str
    passed: bool
    detail: str = ""
    kind: str = "deterministic"


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
    source_feedback_id: int | None
    source_llm_call_id: int
    route: dict[str, Any] = field(default_factory=dict)
    assertions: list[AssertionResult] = field(default_factory=list)
    execution: EvalExecution | None = None
    error: str | None = None
    execution_attempts: int = 1

    @property
    def passed(self) -> bool:
        """Whether every assertion passed and no runner error occurred."""
        return self.error is None and all(
            assertion.passed for assertion in self.assertions
        )

    @property
    def errored(self) -> bool:
        """Whether no verdict was reached because execution never succeeded.

        Distinct from a failed case: the model was never successfully asked, so
        the outcome says nothing about its quality and must not be scored as
        though it did.
        """
        return self.error is not None

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
            max_tokens=(
                int(cached["max_tokens"])
                if cached.get("max_tokens", None) is not None
                else None
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
                "max_tokens": getattr(result, "max_tokens", None),
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
    model: str | None = None,
    max_tool_iterations: int = 5,
    reasoning_effort: str | None = None,
    temperature: float | None = EVAL_TEMPERATURE,
    cache: EvalCache | None = None,
    refresh_cache: bool = False,
) -> EvalResult:
    """Run one eval case and evaluate its deterministic assertions.

    Args:
        case: The case to run.
        model: Model override. ``None`` uses the route this case's feature
            resolves to in production, which is the point of the exercise —
            an eval passing on a model the daemon never calls proves nothing.
        max_tool_iterations: Tool loop ceiling.
        reasoning_effort: Effort override. ``None`` takes production's.
        temperature: Pass ``None`` for models that reject the parameter.
        cache: Optional response cache.
        refresh_cache: Ignore and overwrite cached responses.

    Returns:
        The completed result, including any runner error.
    """
    inherit_effort = reasoning_effort == PRODUCTION_EFFORT
    if inherit_effort:
        reasoning_effort = None
    if model is None:
        route = production_route(case.feature)
        model = str(route["model"])
        # Temperature travels with the model, not with the effort override. A
        # route pins it off because that model rejects or misbehaves with it —
        # Opus 5 fails the request outright and silently falls back to DeepSeek,
        # so the harness reports a score for a model it never called.
        temperature = route.get("temperature")
        if inherit_effort:
            reasoning_effort = route.get("reasoning_effort")
    result = EvalResult(
        case_id=case.id,
        feature=case.feature,
        case_kind=case.case_kind,
        model=model,
        source_feedback_id=case.source_feedback_id,
        source_llm_call_id=case.source_llm_call_id,
    )
    for attempt in range(EVAL_EXECUTION_ATTEMPTS):
        try:
            _execute_case(
                case,
                result,
                model=model,
                max_tool_iterations=max_tool_iterations,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                cache=cache,
                # A retry must not be served the response that just failed.
                refresh_cache=refresh_cache or attempt > 0,
            )
            result.error = None
            break
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.execution_attempts = attempt + 1
            if attempt + 1 < EVAL_EXECUTION_ATTEMPTS:
                logger.warning(
                    "Eval case %s execution failed (attempt %d/%d), retrying: %s",
                    case.id,
                    attempt + 1,
                    EVAL_EXECUTION_ATTEMPTS,
                    result.error,
                )
    if result.error is not None:
        return result

    # Assertions are deterministic, so they are evaluated once rather than
    # retried — but a malformed assertion must still be recorded rather than
    # aborting the whole run partway through a suite.
    try:
        result.assertions = run_assertions(case.assertions, execution=result.execution)
        if all(assertion.passed for assertion in result.assertions):
            result.assertions.extend(
                run_judge_assertions(
                    case,
                    result.execution,
                    cache=cache,
                    refresh_cache=refresh_cache,
                )
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _execute_case(
    case: EvalCase,
    result: EvalResult,
    *,
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: EvalCache | None,
    refresh_cache: bool,
) -> None:
    """Obtain one model response for a case, populating route and execution.

    Separated from assertion evaluation so a transient provider fault can be
    retried without re-running deterministic assertions, and so an assertion
    bug cannot be mistaken for one.
    """
    if case.feature == "chat":
        result.route = _eval_route(
            feature=case.feature,
            primary=model,
            fallback_models=[],
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            source="eval_cli",
        )
        execution = _run_chat_case(
            case,
            model=model,
            max_tool_iterations=max_tool_iterations,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )
    elif case.feature == "verification_judge":
        from evals.run_verify import run_verification_judge_case

        execution, result.model, result.route = run_verification_judge_case(
            case,
            model=model,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )
    elif case.feature == "nudge":
        from evals.run_nudge import run_nudge_case

        execution, result.model, result.route = run_nudge_case(
            case,
            model=model,
            max_tool_iterations=max_tool_iterations,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )
    elif case.feature == "memory":
        from evals.run_memory import run_memory_case

        execution, result.model, result.route = run_memory_case(
            case,
            model=model,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )
    elif case.feature == "insights":
        from evals.run_insights import run_insights_case

        execution, result.model, result.route = run_insights_case(
            case,
            model=model,
            max_tool_iterations=max_tool_iterations,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )
    else:
        raise ValueError(f"Unsupported eval feature: {case.feature}")
    result.execution = execution


def _eval_route(
    *,
    feature: str,
    primary: str,
    fallback_models: list[str] | None,
    reasoning_effort: str | None,
    temperature: float | None,
    source: str,
) -> dict[str, Any]:
    """Return a JSON-safe route description for one eval case."""
    return {
        "feature": feature,
        "primary": primary,
        "fallback_models": list(fallback_models or []),
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "source": source,
    }


def run_assertions(
    assertions: list[dict[str, Any]],
    execution: EvalExecution,
) -> list[AssertionResult]:
    """Evaluate deterministic assertions against a captured execution."""
    return [_evaluate_assertion(assertion, execution) for assertion in assertions]


def run_judge_assertions(
    case: EvalCase,
    execution: EvalExecution,
    *,
    cache: EvalCache | None = None,
    refresh_cache: bool = False,
) -> list[AssertionResult]:
    """Evaluate optional semantic assertions with one structured judge call."""
    if not case.judge_assertions:
        return []
    raw_text = _call_judge_for_eval(
        case,
        execution,
        cache=cache,
        refresh_cache=refresh_cache,
    )
    try:
        judge_response = JudgeResponse.model_validate_json(raw_text)
    except ValidationError as exc:
        return [
            AssertionResult(
                name="judge_response_valid",
                passed=False,
                detail=_judge_validation_detail(exc, raw_text),
                kind="judge",
            )
        ]
    results_by_name: dict[str, JudgeAssertionResult] = {}
    duplicate_names: set[str] = set()
    for item in judge_response.results:
        if item.name in results_by_name:
            duplicate_names.add(item.name)
        results_by_name[item.name] = item

    expected_names = [str(assertion["name"]) for assertion in case.judge_assertions]
    assertion_results: list[AssertionResult] = []
    for name in expected_names:
        if name in duplicate_names:
            assertion_results.append(
                AssertionResult(
                    name=name,
                    passed=False,
                    detail="Judge returned multiple results for this assertion.",
                    kind="judge",
                )
            )
            continue
        judged = results_by_name.get(name)
        if judged is None:
            assertion_results.append(
                AssertionResult(
                    name=name,
                    passed=False,
                    detail="Judge response omitted this assertion.",
                    kind="judge",
                )
            )
            continue
        detail = "" if judged.passed else _judge_failure_detail(judged)
        assertion_results.append(
            AssertionResult(
                name=name,
                passed=judged.passed,
                detail=detail,
                kind="judge",
            )
        )

    extra_names = sorted(set(results_by_name) - set(expected_names))
    if extra_names:
        assertion_results.append(
            AssertionResult(
                name="judge_no_extra_results",
                passed=False,
                detail=f"Unexpected judge result(s): {extra_names}",
                kind="judge",
            )
        )
    return assertion_results


def _judge_failure_detail(result: JudgeAssertionResult) -> str:
    """Format a failed semantic judge result for result tables."""
    parts = [result.reason.strip()]
    evidence = result.evidence.strip()
    if evidence:
        parts.append(f"evidence: {evidence}")
    return "; ".join(part for part in parts if part)


def _judge_validation_detail(exc: ValidationError, raw_text: str) -> str:
    """Format a Pydantic validation failure with a snippet of the raw output."""
    snippet = raw_text.strip().replace("\n", " ")
    if len(snippet) > 500:
        snippet = snippet[:500] + "…"
    return f"Pydantic validation failed: {exc}; raw: {snippet!r}"


def _response_format_cache_key(
    response_format: dict[str, Any] | type[BaseModel] | None,
) -> Any:
    """Return a JSON-safe cache key fragment for a response format."""
    if response_format is None:
        return None
    if isinstance(response_format, dict):
        return response_format
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return {
            "type": "pydantic",
            "name": response_format.__name__,
            "schema": response_format.model_json_schema(),
        }
    return str(response_format)


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
            (
                f"fb#{result.source_feedback_id}/call#{result.source_llm_call_id}"
                if result.source_feedback_id is not None
                else f"call#{result.source_llm_call_id}"
            ),
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


def score_counts(results: list[EvalResult]) -> tuple[int, int, int, float]:
    """Return (passed, failed, errored, accuracy) with errors excluded.

    Accuracy is computed only over cases that actually reached a verdict. A
    case whose execution never succeeded says nothing about the model, and
    folding it into the denominator makes a provider hiccup look like a
    quality difference in exactly the comparison evals exist to inform.
    """
    errored = sum(1 for result in results if result.errored)
    scored = [result for result in results if not result.errored]
    passed = sum(1 for result in scored if result.passed)
    failed = len(scored) - passed
    accuracy = (passed / len(scored) * 100.0) if scored else 0.0
    return passed, failed, errored, accuracy


def _format_pass_fail_summary(results: list[EvalResult]) -> str:
    """Build a compact pass/fail summary for the result footer."""
    passed, failed, errored, accuracy = score_counts(results)
    summary = f"Accuracy: {accuracy:.1f}% | Passed: {passed} | Failed: {failed}"
    if errored:
        summary += f" | Errored: {errored}"
    return summary


def _format_failed_case_summary(results: list[EvalResult]) -> str | None:
    """Build a compact failed-case list for the result footer."""
    failed_case_ids = [
        result.case_id for result in results if not result.passed and not result.errored
    ]
    if not failed_case_ids:
        return None
    return "Failed cases: " + ", ".join(failed_case_ids)


def stability_rows(results: list[EvalResult]) -> list[tuple[str, int, int, bool]]:
    """Return (case_id, passes, runs, flaky) for cases run more than once.

    A case that passes on some runs and fails on others is the most dangerous
    result an eval can produce, because a single run reports it as a clean
    pass or a clean failure with equal confidence. Surfacing the rate is the
    only way a model comparison built on these cases means anything.

    Args:
        results: All results from a run, possibly several per case.

    Returns:
        One tuple per repeated case, in first-seen order.
    """
    order: list[str] = []
    tally: dict[str, list[int]] = {}
    for result in results:
        if result.case_id not in tally:
            order.append(result.case_id)
            tally[result.case_id] = [0, 0]
        tally[result.case_id][1] += 1
        if result.passed:
            tally[result.case_id][0] += 1
    rows: list[tuple[str, int, int, bool]] = []
    for case_id in order:
        passes, runs = tally[case_id]
        if runs > 1:
            rows.append((case_id, passes, runs, 0 < passes < runs))
    return rows


def _format_errored_case_summary(results: list[EvalResult]) -> str | None:
    """Build a compact errored-case list for the result footer."""
    errored_case_ids = [result.case_id for result in results if result.errored]
    if not errored_case_ids:
        return None
    return "Errored cases: " + ", ".join(errored_case_ids)


def _summary_rows(
    results: list[EvalResult],
    *,
    text_cls: type | None = None,
) -> list[tuple[str, Any]]:
    """Build rich-summary rows for the eval footer."""
    passed, failed, errored, accuracy = score_counts(results)
    rows: list[tuple[str, Any]] = [
        ("Accuracy", _render_accuracy_value(accuracy, text_cls=text_cls)),
        ("Passed", str(passed)),
        ("Failed", str(failed)),
    ]
    if errored:
        rows.append(("Errored", str(errored)))
    failed_summary = _format_failed_case_summary(results)
    if failed_summary is not None:
        rows.append(("Failed Cases", failed_summary.removeprefix("Failed cases: ")))
    errored_summary = _format_errored_case_summary(results)
    if errored_summary is not None:
        rows.append(("Errored Cases", errored_summary.removeprefix("Errored cases: ")))
    stability = stability_rows(results)
    if stability:
        rows.append(
            (
                "Stability",
                " | ".join(
                    f"{case_id} {passes}/{runs}{' FLAKY' if flaky else ''}"
                    for case_id, passes, runs, flaky in stability
                ),
            )
        )
        flaky_ids = [case_id for case_id, _p, _r, flaky in stability if flaky]
        if flaky_ids:
            rows.append(("Flaky Cases", ", ".join(flaky_ids)))
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
    # source_feedback_id is deliberately absent from this set. Silent-failure
    # cases — a suppressed report, a stripped memory block, a skipped tool call
    # — are seeded from the call log because no thumbs-down can ever exist for
    # them, and requiring the field forced a 0 sentinel that claims a feedback
    # row that was never written.
    required = {
        "id",
        "feature",
        "case_kind",
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
    feature = str(raw["feature"])
    if feature == "chat":
        if not all(key in fixture for key in ("today", "context", "turns")):
            raise ValueError(
                f"{path} chat fixture must include today, context, and turns"
            )
    elif feature == "verification_judge":
        if not all(key in fixture for key in ("draft", "evidence", "source_messages")):
            raise ValueError(
                f"{path} {feature} fixture must include draft, evidence, "
                "and source_messages"
            )
        # slim_source_messages appends the draft as the final assistant turn, so
        # the two are the same text in production. Re-seeding a draft without
        # updating the turn leaves the verifier auditing the old one, which
        # looks like a model failure rather than a stale fixture.
        source_messages = fixture["source_messages"]
        trailing = source_messages[-1] if source_messages else {}
        if trailing.get("role") != "assistant":
            raise ValueError(
                f"{path} source_messages must end with the assistant turn "
                "carrying the draft"
            )
        if trailing.get("content") != fixture["draft"]:
            raise ValueError(
                f"{path} source_messages[-1] does not match draft — update both "
                "when re-seeding, or the verifier audits the stale text"
            )
    elif feature == "memory":
        if "report" not in fixture:
            raise ValueError(f"{path} memory fixture must include report")
    elif feature in ("insights", "nudge"):
        has_health_context = "health_data" in fixture or "health_data_text" in fixture
        if (
            not all(key in fixture for key in ("today", "context"))
            or not has_health_context
        ):
            raise ValueError(
                f"{path} {feature} fixture must include today, context, and "
                "health_data or health_data_text"
            )
    else:
        raise ValueError(f"{path} unsupported feature: {feature}")
    return EvalCase(
        id=str(raw["id"]),
        feature=str(raw["feature"]),
        case_kind=str(raw["case_kind"]),
        source_feedback_id=(
            int(raw["source_feedback_id"])
            if raw.get("source_feedback_id") is not None
            else None
        ),
        source_llm_call_id=int(raw["source_llm_call_id"]),
        derived_from=dict(raw["derived_from"]),
        intent=str(raw["intent"]),
        fixture=fixture,
        assertions=raw["assertions"],
        judge_assertions=_judge_assertions_from_dict(raw, path),
        notes=str(raw.get("notes", "")),
    )


def _judge_assertions_from_dict(
    raw: dict[str, Any],
    path: Path,
) -> list[dict[str, str]]:
    """Validate and normalize optional semantic judge assertions."""
    judge_assertions = raw.get("judge_assertions", [])
    if judge_assertions is None:
        return []
    if not isinstance(judge_assertions, list):
        raise ValueError(f"{path} judge_assertions must be a list")
    normalized: list[dict[str, str]] = []
    for item in judge_assertions:
        if not isinstance(item, dict):
            raise ValueError(f"{path} judge_assertions entries must be objects")
        name = item.get("name")
        statement = item.get("statement")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path} judge assertion missing non-empty name")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError(f"{path} judge assertion {name!r} missing statement")
        normalized.append({"name": name, "statement": statement})
    return normalized


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
        # Fixtures have no database to describe, so a case that depends on
        # coverage has to state it rather than have one computed.
        data_maturity=fixture.get("data_maturity"),
    )
    messages.extend(_fixture_turns(fixture))

    return run_tool_loop(
        case=case,
        fixture=fixture,
        messages=messages,
        tools=all_chat_tools(),
        model=model,
        max_tokens=int(fixture.get("max_tokens", 1024)),
        max_tool_iterations=max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
    )


def run_tool_loop(
    *,
    case: EvalCase,
    fixture: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: EvalCache | None,
    refresh_cache: bool,
    extra_metadata: dict[str, Any] | None = None,
) -> EvalExecution:
    """Drive a tool-calling LLM loop against fixture-seeded tool results.

    Args:
        case: Eval case providing id / source_feedback_id for trace metadata.
        fixture: Fixture dict; tool results are resolved against ``db_seed``.
        messages: Initial messages (system + user). Mutated as turns are added.
        tools: Tool list passed to the model.
        model: Model identifier.
        max_tokens: Per-call max_tokens.
        max_tool_iterations: Hard cap on tool-call iterations.
        reasoning_effort: Reasoning effort knob, see ``src/llm.py``.
        temperature: Sampling temperature; ``None`` to omit.
        cache: Optional eval cache.
        refresh_cache: When true, bypass cached hits.
        extra_metadata: Extra trace metadata merged into each call.

    Returns:
        Aggregated ``EvalExecution`` with tokens, latency, cost, and the final
        assistant text.
    """
    extras = dict(extra_metadata or {})
    captured: list[CapturedToolCall] = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    latency_s = 0.0
    cost = 0.0
    cache_hits = 0
    cache_misses = 0
    last_result: Any = None

    def _accumulate(result: Any, cache_hit: bool) -> None:
        nonlocal input_tokens, output_tokens, total_tokens
        nonlocal latency_s, cost, cache_hits, cache_misses
        if cache_hit:
            cache_hits += 1
        else:
            cache_misses += 1
        input_tokens += int(getattr(result, "input_tokens", 0) or 0)
        output_tokens += int(getattr(result, "output_tokens", 0) or 0)
        total_tokens += int(getattr(result, "total_tokens", 0) or 0)
        latency_s += float(getattr(result, "latency_s", 0.0) or 0.0)
        if getattr(result, "cost", None) is not None:
            cost += float(result.cost)

    for iteration in range(max_tool_iterations):
        last_result, cache_hit = _call_llm_for_eval(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=tools,
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                **extras,
                "iteration": iteration,
            },
            cache=cache,
            refresh_cache=refresh_cache,
        )
        _accumulate(last_result, cache_hit)

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
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=None,
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                **extras,
                "iteration": "final_synthesis",
            },
            cache=cache,
            refresh_cache=refresh_cache,
        )
        _accumulate(last_result, cache_hit)

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
    response_format: dict[str, Any] | type[BaseModel] | None = None,
    fallback_models: list[str] | None = None,
) -> tuple[llm.LLMResult, bool]:
    """Call the LLM for an eval case with optional request caching."""
    # reasoning_effort drives both Anthropic native reasoning and DeepSeek
    # thinking (translated inside call_llm), so it alone partitions the cache
    # correctly across providers.
    request = {
        "cache_schema_version": EVAL_CACHE_SCHEMA_VERSION,
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
        "tools": tools,
        "response_format": _response_format_cache_key(response_format),
        "fallback_models": fallback_models,
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
        response_format=response_format,
        fallback_models=fallback_models,
        request_type="",
        metadata=metadata,
    )
    if cache is not None:
        cache.put(request, result)
    return result, False


def _call_judge_for_eval(
    case: EvalCase,
    execution: EvalExecution,
    *,
    cache: EvalCache | None,
    refresh_cache: bool,
) -> str:
    """Run the semantic judge once and return its raw response text."""
    judge_model = os.environ.get("ZDROWSKIT_EVAL_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    messages = [
        {
            "role": "system",
            "content": _judge_system_prompt(),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "case_id": case.id,
                    "intent": case.intent,
                    "conversation_turns": case.fixture.get("turns", []),
                    "candidate_response": execution.text,
                    # Much of what these prompts produce is written through
                    # tools rather than said: a log entry, a strategy edit, a
                    # SQL query. Without these the judge sees only the chat
                    # reply and reports a log-format assertion as "no log
                    # entry at all" while the entry sits in the tool call.
                    "candidate_tool_calls": [
                        {"name": call.name, "arguments": call.arguments}
                        for call in execution.tool_calls
                    ],
                    "assertions": case.judge_assertions,
                },
                indent=2,
            ),
        },
    ]
    result, _cache_hit = _call_llm_for_eval(
        messages=messages,
        model=judge_model,
        max_tokens=EVAL_JUDGE_MAX_TOKENS,
        reasoning_effort=None,
        temperature=EVAL_JUDGE_TEMPERATURE,
        tools=None,
        response_format=JudgeResponse,
        fallback_models=[],
        metadata={
            "eval_case_id": case.id,
            "source_feedback_id": case.source_feedback_id,
            "judge": True,
        },
        cache=cache,
        refresh_cache=refresh_cache,
    )
    return result.text


def _judge_system_prompt() -> str:
    """Return the generic semantic judge prompt."""
    return (PROMPTS_DIR / "eval_judge_prompt.md").read_text(encoding="utf-8")


def _build_context(fixture: dict[str, Any]) -> dict[str, str]:
    context = {key: str(value) for key, value in fixture["context"].items()}
    context["prompt"] = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
    # Evals pin the default persona so a change to the operator's own soul.md
    # cannot silently move eval results.
    context["soul"] = llm_context.load_default_soul()
    context["conduct"] = llm_context.load_prompt_text(llm_context.CONDUCT_PROMPT)
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
        if tool_call.arguments.get("action") not in {"append", "replace_section"}:
            return (
                "Not proposed: invalid context update. Check the update_context "
                "schema, target section, and compact log-entry rules before "
                "retrying."
            )
        normalized_edit = context_edit_from_tool_call(
            SimpleNamespace(
                id=tool_call.tool_call_id,
                function=SimpleNamespace(
                    name=tool_call.name,
                    arguments=json.dumps(tool_call.arguments),
                ),
            )
        )
        if normalized_edit is None:
            return (
                "Not proposed: invalid context update. Check the update_context "
                "schema, target section, and compact log-entry rules before "
                "retrying."
            )
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
    if atype == "memory_present":
        return _assert_memory_present(name, execution)
    if atype == "memory_contains":
        return _assert_memory_contains(name, assertion, execution)
    if atype == "memory_absent":
        return _assert_memory_absent(name, assertion, execution)
    if atype == "memory_bullet_max":
        return _assert_memory_bullet_max(name, assertion, execution)
    if atype == "word_count_max":
        return _assert_word_count_max(name, assertion, execution)
    if atype == "visible_char_count_max":
        return _assert_visible_char_count_max(name, assertion, execution)
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


def _assert_visible_char_count_max(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    """Cap the length of what the user actually receives.

    ``word_count_max`` counts the whole response, chart source included, so it
    cannot express the report's contract: a body that fits a phone notification.
    Charts are sent as separate images and any stray memory block is stripped
    before delivery, so neither counts against the budget.
    """
    max_chars = int(assertion["max_chars"])
    visible = re.sub(
        r"\s*<memory>.*?</memory>\s*",
        "",
        strip_charts(execution.text),
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    count = len(visible)
    return AssertionResult(
        name=name,
        passed=count <= max_chars,
        detail=f"{count} chars, max {max_chars}",
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


def _assert_memory_present(name: str, execution: EvalExecution) -> AssertionResult:
    memory = _memory_block(execution.text)
    return AssertionResult(
        name=name,
        passed=memory is not None,
        detail="" if memory is not None else "No <memory> block found.",
    )


def _assert_memory_contains(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    memory = _memory_block(execution.text)
    if memory is None:
        return AssertionResult(
            name=name, passed=False, detail="No <memory> block found."
        )
    missing = [
        pattern
        for pattern in assertion.get("patterns", [])
        if not _text_matches(memory, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not missing,
        detail="" if not missing else f"Missing from memory: {missing}",
    )


def _assert_memory_absent(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    memory = _memory_block(execution.text)
    if memory is None:
        return AssertionResult(
            name=name, passed=False, detail="No <memory> block found."
        )
    present = [
        pattern
        for pattern in assertion.get("patterns", [])
        if _text_matches(memory, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not present,
        detail="" if not present else f"Present in memory: {present}",
    )


def _assert_memory_bullet_max(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    """Cap the number of bullets carried forward.

    The limit is the point of the memory block, not a formatting preference:
    every line is replayed into later prompts for weeks, and the historical
    failure was five bullets of prescriptions the user had never seen.
    """
    memory = _memory_block(execution.text)
    if memory is None:
        return AssertionResult(
            name=name, passed=False, detail="No <memory> block found."
        )
    limit = int(assertion.get("max", 2))
    bullets = [
        line
        for line in memory.splitlines()
        if line.lstrip().startswith(("-", "*", "•"))
    ]
    return AssertionResult(
        name=name,
        passed=len(bullets) <= limit,
        detail=(
            "" if len(bullets) <= limit else f"{len(bullets)} bullets, limit {limit}"
        ),
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


def _memory_block(text: str) -> str | None:
    match = re.search(r"<memory>(.*?)</memory>", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


_MATCHER_KEYS = frozenset(
    {"equals", "contains", "not_contains", "regex", "not_regex", "case_sensitive"}
)


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        # An unrecognised key used to be ignored, so a typo or an invented
        # matcher made the assertion vacuously true — a test that reports
        # success while checking nothing is worse than no test at all.
        unknown = sorted(set(expected) - _MATCHER_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown matcher key(s) {unknown}; supported: {sorted(_MATCHER_KEYS)}"
            )
        text = str(actual or "")
        flags = 0 if expected.get("case_sensitive") else re.IGNORECASE
        if "equals" in expected and actual != expected["equals"]:
            return False
        contains = expected.get("contains", [])
        if contains and any(str(item).lower() not in text.lower() for item in contains):
            return False
        not_contains = expected.get("not_contains", [])
        if not_contains and any(
            str(item).lower() in text.lower() for item in not_contains
        ):
            return False
        regex = expected.get("regex")
        if regex and re.search(str(regex), text, flags) is None:
            return False
        not_regex = expected.get("not_regex")
        if not_regex and re.search(str(not_regex), text, flags) is not None:
            return False
        return True
    if isinstance(expected, str) and expected.startswith("re:"):
        return re.search(expected[3:], str(actual or ""), re.IGNORECASE) is not None
    return actual == expected


def _text_matches(text: str, pattern: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.IGNORECASE) is not None
    return pattern.lower() in text.lower()
