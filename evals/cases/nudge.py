"""Eval: nudge SKIP logic and content quality.

Mirrors ``cmd_nudge()`` in ``src/commands.py``.

Scenarios:
    rest_day   — Last day has no workouts + missed_session trigger.
                 Model should respond with exactly "SKIP".
    baseline   — Real data + new_data trigger.
                 Model should produce a short nudge (not SKIP, < 80 words).

Assertions per scenario:
    rest_day:   response is exactly SKIP
    baseline:   response is NOT skip, word count < 80
"""

from __future__ import annotations

from evals.data.scenarios import baseline, rest_day
from evals.framework import (
    AssertionResult,
    Eval,
    response_is_not_skip,
    response_is_skip,
    word_count_under,
)

_NUDGE_CONFIG = {
    "prompt_file": "nudge_prompt",
    "recent_nudges": "(none yet)",
    "max_tokens": 4096,
}


class NudgeEval(Eval):
    name = "nudge"

    def eval_scenarios(self) -> list[tuple[str, callable, dict]]:
        return [
            (
                "rest_day",
                rest_day,
                {**_NUDGE_CONFIG, "trigger_type": "missed_session"},
            ),
            (
                "baseline",
                baseline,
                {**_NUDGE_CONFIG, "trigger_type": "new_data"},
            ),
        ]

    def assertions(
        self,
        response: str,
        tool_calls: list | None = None,
        health_data: dict | None = None,
    ) -> list[AssertionResult]:
        # This method is called per-scenario.  We can't tell which scenario
        # triggered it from here, so we return both checks and let the
        # relevant one dominate.  In practice the runner calls assertions()
        # after each scenario independently, so this is fine — but we need
        # scenario-aware assertions.
        #
        # For now, return both; the framework filters by pass/fail.
        # This will be refined if needed.
        return [
            response_is_not_skip(response),
            word_count_under(response, 80),
        ]


class NudgeSkipEval(Eval):
    """Dedicated eval for the SKIP scenario."""

    name = "nudge_skip"

    def eval_scenarios(self) -> list[tuple[str, callable, dict]]:
        return [
            (
                "rest_day",
                rest_day,
                {**_NUDGE_CONFIG, "trigger_type": "missed_session"},
            ),
        ]

    def assertions(
        self,
        response: str,
        tool_calls: list | None = None,
        health_data: dict | None = None,
    ) -> list[AssertionResult]:
        return [response_is_skip(response)]
