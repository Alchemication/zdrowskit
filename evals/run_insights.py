"""Runner for direct insights-writer eval cases.

Exercises the insights prompt and ``run_sql`` tool loop only. It intentionally
does not call verification/rewrite, render charts, save reports, update
history, or send Telegram messages.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import llm_context  # noqa: E402
import llm_health  # noqa: E402
from config import MAX_TOKENS_INSIGHTS, PROMPTS_DIR  # noqa: E402
from tools import run_sql_tool  # noqa: E402


def run_insights_case(
    case: Any,
    *,
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one insights-writer case and return execution, model, and route."""
    from evals.framework import (  # local import to avoid cycle
        _eval_route,
    )

    fixture = case.fixture
    today = date.fromisoformat(str(fixture["today"]))
    context = _build_context(fixture)
    health_data_text = _health_data_text(fixture, today=today)
    health_data = fixture.get("health_data", {})
    week_complete = bool(
        fixture.get(
            "week_complete",
            health_data.get("week_complete", False)
            if isinstance(health_data, dict)
            else False,
        )
    )
    week = str(fixture.get("week", "current"))
    messages: list[dict[str, Any]] = llm_context.build_messages(
        context,
        health_data_text=health_data_text,
        baselines=fixture.get("baselines"),
        milestones=fixture.get("milestones"),
        week_complete=week_complete,
        today=today,
    )

    execution = _run_tool_loop(
        case=case,
        fixture=fixture,
        messages=messages,
        model=model,
        max_tool_iterations=max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
    )
    route = _eval_route(
        feature="insights",
        primary=model,
        fallback_models=[],
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        source="eval_cli",
    )
    route["week"] = week
    return execution, model, route


def _build_context(fixture: dict[str, Any]) -> dict[str, str]:
    """Build prompt context for an insights eval fixture."""
    context = {key: str(value) for key, value in fixture["context"].items()}
    context["prompt"] = (PROMPTS_DIR / "insights_prompt.md").read_text(encoding="utf-8")
    soul_path = PROMPTS_DIR / "soul.md"
    context["soul"] = (
        soul_path.read_text(encoding="utf-8")
        if soul_path.exists()
        else llm_context.load_prompt_text("default_soul")
    )
    if "review_facts" in fixture:
        context["review_facts"] = str(fixture["review_facts"])
    return context


def _health_data_text(fixture: dict[str, Any], *, today: date) -> str:
    """Return rendered or pre-rendered health data for an insights fixture."""
    rendered = fixture.get("health_data_text")
    if rendered is not None:
        return str(rendered)
    return llm_health.render_health_data(
        fixture.get("health_data", {}),
        prompt_kind="report",
        week=str(fixture.get("week", "current")),
        today=today,
    )


def _run_tool_loop(
    *,
    case: Any,
    fixture: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any,
    refresh_cache: bool,
) -> Any:
    """Run the insights SQL loop against fixture-seeded tool results."""
    from evals.framework import (  # local import to avoid cycle
        EvalExecution,
        _assistant_message,
        _call_llm_for_eval,
        _capture_tool_call,
        _eval_tool_result,
        _result_tool_calls,
    )

    tools = run_sql_tool()
    captured = []
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
            max_tokens=int(fixture.get("max_tokens", MAX_TOKENS_INSIGHTS)),
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=tools,
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                "stage": "insights",
                "week": fixture.get("week", "current"),
                "iteration": iteration,
            },
            cache=cache,
            refresh_cache=refresh_cache,
        )
        cache_hits += 1 if cache_hit else 0
        cache_misses += 0 if cache_hit else 1
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
            max_tokens=int(fixture.get("max_tokens", MAX_TOKENS_INSIGHTS)),
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=None,
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                "stage": "insights",
                "week": fixture.get("week", "current"),
                "iteration": "final_synthesis",
            },
            cache=cache,
            refresh_cache=refresh_cache,
        )
        cache_hits += 1 if cache_hit else 0
        cache_misses += 0 if cache_hit else 1
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
