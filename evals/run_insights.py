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
    from evals.framework import (
        _eval_route,
        run_tool_loop,
    )  # local import to avoid cycle

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
        # Fixtures have no database to describe, so a case that depends on
        # coverage has to state it rather than have one computed.
        data_maturity=fixture.get("data_maturity"),
    )

    execution = run_tool_loop(
        case=case,
        fixture=fixture,
        messages=messages,
        tools=run_sql_tool(),
        model=model,
        max_tokens=int(fixture.get("max_tokens", MAX_TOKENS_INSIGHTS)),
        max_tool_iterations=max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
        extra_metadata={"stage": "insights", "week": week},
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
    # Evals pin the default persona so a change to the operator's own soul.md
    # cannot silently move eval results.
    context["soul"] = llm_context.load_default_soul()
    context["conduct"] = llm_context.load_prompt_text(llm_context.CONDUCT_PROMPT)
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
