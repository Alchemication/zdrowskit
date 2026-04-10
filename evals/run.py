"""Run feedback-derived evals.

Usage:
    uv run python -m evals.run
    uv run python -m evals.run chat_log_life_disruption
    uv run python -m evals.run --feature chat
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from evals.framework import (
    DEFAULT_MODEL,
    EvalCase,
    load_cases,
    print_result_details,
    print_results,
    run_case,
)


def select_cases(
    cases: list[EvalCase],
    *,
    case_ids: list[str] | None = None,
    feature: str | None = None,
) -> list[EvalCase]:
    """Filter eval cases by id and/or feature."""
    selected = cases
    if case_ids:
        wanted = set(case_ids)
        selected = [case for case in selected if case.id in wanted]
        missing = sorted(wanted - {case.id for case in selected})
        if missing:
            available = ", ".join(case.id for case in cases)
            raise ValueError(f"Unknown case(s): {missing}. Available: {available}")
    if feature:
        selected = [case for case in selected if case.feature == feature]
        if not selected:
            available = ", ".join(sorted({case.feature for case in cases}))
            raise ValueError(
                f"No cases for feature '{feature}'. Available: {available}"
            )
    return selected


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Run feedback-derived evals.")
    parser.add_argument("cases", nargs="*", help="Case IDs to run. Default: all.")
    parser.add_argument("--feature", help="Run only cases for this feature.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="litellm model string.")
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print final text and captured tools for failed cases.",
    )
    parser.add_argument(
        "--max-tool-iterations",
        type=int,
        default=5,
        help="Maximum tool loop iterations before final synthesis.",
    )
    args = parser.parse_args()

    try:
        selected = select_cases(
            load_cases(),
            case_ids=args.cases or None,
            feature=args.feature,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    results = _run_selected_cases(
        selected,
        model=args.model,
        max_tool_iterations=args.max_tool_iterations,
    )
    print_results(results)
    if args.details:
        print_result_details(results)
    if not all(result.passed for result in results):
        sys.exit(1)


def _run_selected_cases(
    cases: Iterable[EvalCase],
    *,
    model: str,
    max_tool_iterations: int,
):
    selected = list(cases)
    if len(selected) <= 1:
        return [
            run_case(
                case,
                model=model,
                max_tool_iterations=max_tool_iterations,
            )
            for case in selected
        ]

    results = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task_id = progress.add_task("Running feedback evals", total=len(selected))
        for case in selected:
            progress.update(task_id, description=f"[bold]{case.id}[/bold]")
            results.append(
                run_case(
                    case,
                    model=model,
                    max_tool_iterations=max_tool_iterations,
                )
            )
            progress.advance(task_id)
    return results


if __name__ == "__main__":
    main()
