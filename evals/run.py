"""Run zdrowskit AI evals against one or more models.

Each eval builds prompts from pinned blueprint data (committed snapshots of
real context files and health data), applies named scenario perturbations,
sends to the LLM, and asserts on the response structure.

Usage:
    uv run python -m evals.run                          # all evals, default model (opus)
    uv run python -m evals.run report                   # one eval, all its scenarios
    uv run python -m evals.run nudge nudge_skip         # multiple evals

Model selection (full litellm model string, comma-separated for comparison):
    uv run python -m evals.run --model anthropic/claude-sonnet-4-6
    uv run python -m evals.run --model anthropic/claude-sonnet-4-6,anthropic/claude-haiku-4-5-20251001
    uv run python -m evals.run --model openai/gpt-4o

Scenario filtering:
    uv run python -m evals.run --scenario baseline      # one scenario across all evals
    uv run python -m evals.run report --scenario no_runs_week

Reasoning effort (forwarded to call_llm):
    uv run python -m evals.run --reasoning-effort low

Combined:
    uv run python -m evals.run report --scenario baseline --model anthropic/claude-sonnet-4-6

Other tools:
    uv run python -m evals.data.extract                 # refresh pinned blueprints from live data
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is importable.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from evals.cases.chat_sql import ChatSqlEval  # noqa: E402
from evals.cases.nudge import NudgeEval, NudgeSkipEval  # noqa: E402
from evals.cases.report import ReportEval  # noqa: E402
from evals.framework import DEFAULT_MODEL, EvalResult, print_results  # noqa: E402

ALL_EVALS = [
    ReportEval(),
    NudgeEval(),
    NudgeSkipEval(),
    ChatSqlEval(),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run zdrowskit AI evals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "evals",
        nargs="*",
        help="Eval names to run (default: all).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "Model(s) as full litellm string. "
            "Comma-separated for comparison. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Run only this scenario across selected evals.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="Reasoning effort hint forwarded to the LLM.",
    )
    args = parser.parse_args()

    # Filter evals.
    if args.evals:
        names = set(args.evals)
        selected = [e for e in ALL_EVALS if e.name in names]
        unknown = names - {e.name for e in selected}
        if unknown:
            all_names = [e.name for e in ALL_EVALS]
            print(
                f"Unknown eval(s): {unknown}. Available: {all_names}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        selected = ALL_EVALS

    models = [m.strip() for m in args.model.split(",")]
    results: list[EvalResult] = []

    total_scenarios = sum(ev.scenario_count(args.scenario) for ev in selected) * len(
        models
    )

    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task_id = progress.add_task("Starting...", total=total_scenarios)

        for model in models:
            for ev in selected:
                ev_results = ev.run(
                    model=model,
                    reasoning_effort=args.reasoning_effort,
                    scenario_filter=args.scenario,
                    progress=progress,
                    task_id=task_id,
                )
                results.extend(ev_results)

    print()
    print_results(results)

    if not all(r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
