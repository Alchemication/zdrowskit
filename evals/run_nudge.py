"""Runner for direct nudge eval cases.

Exercises the nudge prompt and ``run_sql`` tool loop only. It intentionally
does not call verification/rewrite, render charts, save nudges, or send
Telegram messages — a suppressed nudge and a nudge that was never worth
sending look identical downstream, and these cases are about what the writer
produced.
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
from config import MAX_TOKENS_NUDGE, PROMPTS_DIR  # noqa: E402
from tools import run_sql_tool  # noqa: E402

# Context keys cmd_nudge injects beyond the context files themselves. A nudge
# reacts to what just happened and to what it already said, so an eval that
# omits these is not exercising the production prompt.
_NUDGE_CONTEXT_DEFAULTS = {
    "recent_nudges": "(none yet)",
    "trigger_type": "new_data",
    "trigger_context": "(no additional detail)",
    "last_coach_summary": "(no recent coach review)",
}


def run_nudge_case(
    case: Any,
    *,
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one nudge case and return execution, model, and route.

    Args:
        case: The eval case to run.
        model: Model to call.
        max_tool_iterations: Tool loop ceiling.
        reasoning_effort: Effort passed to the model.
        temperature: Sampling temperature, or None to omit.
        cache: Optional response cache.
        refresh_cache: Ignore and overwrite cached responses.

    Returns:
        Tuple of (execution, model, route).
    """
    from evals.framework import _eval_route, run_tool_loop  # local import: cycle

    fixture = case.fixture
    today = date.fromisoformat(str(fixture["today"]))
    context = _build_context(fixture)
    health_data_text = _health_data_text(fixture, today=today)

    messages: list[dict[str, Any]] = llm_context.build_messages(
        context,
        health_data_text=health_data_text,
        today=today,
        # Fixtures have no database to describe, so a case depending on
        # coverage states it rather than having one computed.
        data_maturity=fixture.get("data_maturity"),
    )

    execution = run_tool_loop(
        case=case,
        fixture=fixture,
        messages=messages,
        tools=run_sql_tool(),
        model=model,
        max_tokens=int(fixture.get("max_tokens", MAX_TOKENS_NUDGE)),
        max_tool_iterations=max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
        extra_metadata={"stage": "nudge", "trigger": context["trigger_type"]},
    )
    route = _eval_route(
        feature="nudge",
        primary=model,
        fallback_models=[],
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        source="eval_cli",
    )
    return execution, model, route


def _build_context(fixture: dict[str, Any]) -> dict[str, str]:
    """Build prompt context for a nudge eval fixture."""
    context = {key: str(value) for key, value in fixture["context"].items()}
    context["prompt"] = (PROMPTS_DIR / "nudge_prompt.md").read_text(encoding="utf-8")
    # Evals pin the default persona so a change to the operator's own soul.md
    # cannot silently move eval results.
    context["soul"] = llm_context.load_default_soul()
    context["conduct"] = llm_context.load_prompt_text(llm_context.CONDUCT_PROMPT)
    for key, default in _NUDGE_CONTEXT_DEFAULTS.items():
        context.setdefault(key, default)
    return context


def _health_data_text(fixture: dict[str, Any], *, today: date) -> str:
    """Return rendered or pre-rendered health data for a nudge fixture."""
    rendered = fixture.get("health_data_text")
    if rendered is not None:
        return str(rendered)
    return llm_health.render_health_data(
        fixture.get("health_data", {}),
        prompt_kind="nudge",
        today=today,
    )
