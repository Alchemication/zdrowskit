"""Runner for weekly-memory eval cases.

Memory used to be a section of the report, so the only way to score it was to
generate a whole report first and hope the defect reproduced. It is its own
call now, which makes the fixture small and the failure legible: a finished
report goes in, at most two bullets come out, and every rule the block has to
respect is checkable against text the fixture states explicitly.

There is no tool loop here. The call sees a report that has already been
written and verified; it has nothing left to look up.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import MAX_TOKENS_MEMORY  # noqa: E402
from memory_writer import build_memory_messages  # noqa: E402


def run_memory_case(
    case: Any,
    *,
    model: str,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one memory case and return execution, model, and route.

    Args:
        case: The eval case to run.
        model: Model to call.
        reasoning_effort: Effort passed to the model.
        temperature: Sampling temperature, or None to omit.
        cache: Optional response cache.
        refresh_cache: Ignore and overwrite cached responses.

    Returns:
        Tuple of (execution, model, route).
    """
    from evals.framework import _eval_route, run_tool_loop  # local import: cycle

    fixture = case.fixture
    messages = build_memory_messages(
        report=str(fixture["report"]),
        week_label=fixture.get("week_label"),
        review_facts=fixture.get("review_facts"),
        log=fixture.get("log"),
        history=fixture.get("history"),
    )

    execution = run_tool_loop(
        case=case,
        fixture=fixture,
        messages=messages,
        tools=[],
        model=model,
        max_tokens=int(fixture.get("max_tokens", MAX_TOKENS_MEMORY)),
        # One pass, no tools. The loop still needs a single iteration to make
        # the call at all — zero means it returns before speaking to a model.
        max_tool_iterations=1,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=refresh_cache,
        extra_metadata={"stage": "memory"},
    )
    route = _eval_route(
        feature="memory",
        primary=model,
        fallback_models=[],
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        source="eval_cli",
    )
    return execution, model, route
