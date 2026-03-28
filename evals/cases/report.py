"""Eval: weekly report structure and chart rendering.

Mirrors ``cmd_insights()`` in ``src/commands.py``.

Scenarios:
    baseline      — Real data, full week.  The control case.
    no_runs_week  — Only strength sessions, no runs.  Tests handling
                    of missing pace/distance data.

Assertions:
    - 5 required sections present
    - Word count < 600
    - Paces in mm:ss/km (not decimal)
    - <memory> block present and non-empty
    - No markdown tables (Telegram compat)
    - Charts (if any) produce valid PNGs > 1 KB
"""

from __future__ import annotations

from evals.data.scenarios import baseline, no_runs_week
from evals.framework import (
    AssertionResult,
    Eval,
    chart_code_executes,
    has_memory_block,
    has_sections,
    no_markdown_tables,
    pace_format_valid,
    word_count_under,
)

_REQUIRED_SECTIONS = [
    "Week at a Glance",
    "Training Review",
    "Key Metrics",
    "Recovery Status",
]
_PRIORITY_VARIANTS = ["This Week's Priorities", "Next Week"]


class ReportEval(Eval):
    name = "report"

    def eval_scenarios(self) -> list[tuple[str, callable, dict]]:
        return [
            ("baseline", baseline, {"week_complete": True}),
            ("no_runs_week", no_runs_week, {"week_complete": True}),
        ]

    def assertions(
        self,
        response: str,
        tool_calls: list | None = None,
        health_data: dict | None = None,
    ) -> list[AssertionResult]:
        results: list[AssertionResult] = []

        results.append(has_sections(response, _REQUIRED_SECTIONS))

        has_priorities = any(v.lower() in response.lower() for v in _PRIORITY_VARIANTS)
        results.append(
            AssertionResult(
                name="has_priorities_section",
                passed=has_priorities,
                detail=""
                if has_priorities
                else f"Missing one of: {_PRIORITY_VARIANTS}",
            )
        )

        results.append(word_count_under(response, 600))
        results.append(pace_format_valid(response))
        results.append(has_memory_block(response))
        results.append(no_markdown_tables(response))

        # Chart rendering.
        from charts import extract_charts

        blocks = extract_charts(response)
        if blocks and health_data:
            for i, block in enumerate(blocks):
                label = block.title or f"chart_{i}"
                result = chart_code_executes(block.code, health_data)
                result.name = f"chart[{label}]"
                results.append(result)

        return results
