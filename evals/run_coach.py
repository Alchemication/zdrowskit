"""Runner for direct weekly-coach eval cases.

Exercises the coach prompt with its ``run_sql`` and ``update_context`` tools
only. It intentionally does not verify, bundle proposals into Telegram buttons,
apply edits, or record goal-check triggers: the case scores whether the coach
proposed the right thing, not what happened after.
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
from challenges import propose_challenge_tool  # noqa: E402
from cmd_coach import needs_edit_followup  # noqa: E402
from config import MAX_TOKENS_COACH, PROMPTS_DIR  # noqa: E402
from tools import run_sql_tool  # noqa: E402

# Context keys cmd_coach injects beyond the context files. A coach case that
# omits the goal check is not exercising the prompt that ships.
_COACH_CONTEXT_DEFAULTS = {
    "recent_nudges": "(none)",
    "coach_feedback": "(not provided)",
    "review_facts": "(not provided)",
    "goal_check": "No completed weeks with weekly targets yet.\n\n"
    "Review required this week: no.",
    "challenge_status": "No challenge is active.",
    "challenge_history": "No challenges yet.",
}


def run_coach_case(
    case: Any,
    *,
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one coach case and return execution, model, and route.

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
    messages: list[dict[str, Any]] = llm_context.build_messages(
        context,
        health_data_text=str(fixture["health_data_text"]),
        baselines=fixture.get("baselines"),
        milestones=fixture.get("milestones"),
        week_complete=bool(fixture.get("week_complete", True)),
        today=today,
        data_maturity=fixture.get("data_maturity"),
    )

    execution = run_tool_loop(
        case=case,
        fixture=fixture,
        messages=messages,
        tools=_coach_tools(fixture),
        model=model,
        max_tokens=int(fixture.get("max_tokens", MAX_TOKENS_COACH)),
        max_tool_iterations=max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
        extra_metadata={"stage": "coach"},
        followup=_edit_followup,
    )
    route = _eval_route(
        feature="coach",
        primary=model,
        fallback_models=[],
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        source="eval_cli",
    )
    return execution, model, route


def _coach_tools(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the tools cmd_coach offers: challenges only when one is due."""
    tools = run_sql_tool() + llm_context.context_update_tool(allowed_files=["strategy"])
    if fixture.get("challenge_due", False):
        tools = tools + propose_challenge_tool()
    return tools


def _edit_followup(text: str, captured: list[Any]) -> str | None:
    """Mirror cmd_coach's one follow-up when a review forgets its edit call."""
    edits = sum(
        1 for call in captured if call.name in {"update_context", "propose_challenge"}
    )
    if needs_edit_followup(text, edits):
        return llm_context.load_prompt_text("coach_tool_followup")
    return None


def _build_context(fixture: dict[str, Any]) -> dict[str, str]:
    """Build prompt context for a coach eval fixture."""
    context = {key: str(value) for key, value in fixture["context"].items()}
    context["prompt"] = (PROMPTS_DIR / "coach_prompt.md").read_text(encoding="utf-8")
    context["soul"] = llm_context.load_default_soul()
    context["conduct"] = llm_context.load_prompt_text(llm_context.CONDUCT_PROMPT)
    for key, default in _COACH_CONTEXT_DEFAULTS.items():
        context.setdefault(key, default)
    return context
