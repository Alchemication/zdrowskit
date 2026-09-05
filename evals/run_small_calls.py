"""Runners for the three single-call features behind the progress strip.

Weekly targets, the plan-frame decision and the quiet-week check-in are all
one call with no tool loop, so they share this module rather than each getting
a file that would differ by four lines.

What they do not share is what a wrong answer costs. A bad target puts a wrong
number on every notification for a week. A bad plan frame removes the strip
entirely, and does so invisibly. A bad check-in asks someone whose father is in
hospital why they have not been running. None of those produce a thumbs-down,
because none of them look like an error from outside — which is exactly why
they need cases.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import (  # noqa: E402
    MAX_TOKENS_CHECKIN,
    MAX_TOKENS_PLAN_FRAME,
    MAX_TOKENS_TARGETS,
)


def _single_call(
    case: Any,
    messages: list[dict[str, str]],
    *,
    feature: str,
    max_tokens: int,
    model: str,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any,
    refresh_cache: bool,
) -> tuple[Any, str, dict[str, Any]]:
    """Make one tool-free call and wrap it as an eval execution."""
    from evals.framework import _eval_route, run_tool_loop  # local import: cycle

    execution = run_tool_loop(
        case=case,
        fixture=case.fixture,
        messages=messages,
        tools=[],
        model=model,
        max_tokens=int(case.fixture.get("max_tokens", max_tokens)),
        # One pass, no tools. Zero iterations would return without calling.
        max_tool_iterations=1,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
        extra_metadata={"stage": feature},
    )
    route = _eval_route(
        feature=feature,
        primary=model,
        fallback_models=[],
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        source="eval_cli",
    )
    return execution, model, route


def run_targets_case(
    case: Any,
    *,
    model: str,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one weekly-target extraction case.

    The fixture supplies the goal prose and the profile's recorded activity
    types, which is everything the production call is given.
    """
    from weekly_targets import build_targets_messages

    fixture = case.fixture
    activity_types = [
        (str(entry["type"]), int(entry.get("count", 1)))
        for entry in fixture.get("activity_types", [])
    ]
    messages = build_targets_messages(
        str(fixture["goals"]),
        week_start=str(fixture.get("week_start", "2026-08-31")),
        activity_types=activity_types,
    )
    return _single_call(
        case,
        messages,
        feature="targets",
        max_tokens=MAX_TOKENS_TARGETS,
        model=model,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
    )


def run_plan_frame_case(
    case: Any,
    *,
    model: str,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one plan-frame decision case.

    The fixture carries only life context. There is deliberately nowhere to put
    the week's numbers, matching production: a model that could see them would
    start deciding whether they are flattering.
    """
    from plan_frame import build_plan_frame_messages

    fixture = case.fixture
    messages = build_plan_frame_messages(
        me=fixture.get("me"),
        log=fixture.get("log"),
        history=fixture.get("history"),
        today=str(fixture.get("today", "2026-09-04")),
    )
    return _single_call(
        case,
        messages,
        feature="plan_frame",
        max_tokens=MAX_TOKENS_PLAN_FRAME,
        model=model,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
    )


def run_checkin_case(
    case: Any,
    *,
    model: str,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one quiet-week check-in phrasing case.

    Whether to ask was already decided deterministically before this call, so
    the only thing under test is how the question reads.
    """
    from datetime import date

    from quiet_week import WeekActivity, build_checkin_messages

    fixture = case.fixture
    activity = WeekActivity(
        sessions=int(fixture.get("sessions", 0)),
        baseline_per_week=float(fixture.get("baseline_per_week", 4.0)),
        weeks_of_history=int(fixture.get("weeks_of_history", 12)),
        days_elapsed=int(fixture.get("days_elapsed", 5)),
    )
    messages = build_checkin_messages(
        me=fixture.get("me"),
        log=fixture.get("log"),
        history=fixture.get("history"),
        activity=activity,
        today=date.fromisoformat(str(fixture.get("today", "2026-09-04"))),
    )
    return _single_call(
        case,
        messages,
        feature="checkin",
        max_tokens=MAX_TOKENS_CHECKIN,
        model=model,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
    )
