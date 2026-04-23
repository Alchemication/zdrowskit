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

from evals import leaderboard
from evals.framework import (
    DEFAULT_MODEL,
    EVAL_TEMPERATURE,
    EvalCache,
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
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        default="none",
        help="Reasoning effort hint passed to the LLM for eval calls.",
    )
    parser.add_argument(
        "--no-temperature",
        action="store_true",
        help=(
            "Omit the temperature parameter from LLM calls. Required for "
            "models that reject it (e.g. claude-opus-4-7)."
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the local SQLite cache for eval LLM responses.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Ignore cached eval responses and overwrite them with fresh ones.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Record this eval run to the JSONL leaderboard history and regenerate markdown.",
    )
    parser.add_argument(
        "--record-duplicate",
        action="store_true",
        help="Allow recording even if the same run fingerprint already exists.",
    )
    args = parser.parse_args()
    if args.no_cache and args.refresh_cache:
        parser.error("--refresh-cache cannot be used with --no-cache")
    if args.record_duplicate and not args.record:
        parser.error("--record-duplicate requires --record")

    try:
        selected = select_cases(
            load_cases(),
            case_ids=args.cases or None,
            feature=args.feature,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    reasoning_effort = _normalize_reasoning_effort(args.reasoning_effort)
    temperature = None if args.no_temperature else EVAL_TEMPERATURE
    cache = None if args.no_cache else EvalCache()
    results = _run_selected_cases(
        selected,
        model=args.model,
        max_tool_iterations=args.max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=args.refresh_cache,
    )
    print_results(results)
    if args.details:
        print_result_details(results)
    if args.record:
        outcome = leaderboard.record_run(
            results=results,
            case_ids=[case.id for case in selected],
            model=args.model,
            reasoning_effort=reasoning_effort,
            max_tool_iterations=args.max_tool_iterations,
            feature_filter=args.feature,
            allow_duplicate=args.record_duplicate,
        )
        if outcome.recorded:
            print(
                "Recorded leaderboard run "
                f"{outcome.record['run_id']} and regenerated {leaderboard.MARKDOWN_PATH}"
            )
        else:
            print(
                "Matching leaderboard run already recorded "
                f"(run_id={outcome.record['run_id']}); skipped append."
            )
    if not all(result.passed for result in results):
        sys.exit(1)


def _run_selected_cases(
    cases: Iterable[EvalCase],
    *,
    model: str,
    max_tool_iterations: int,
    reasoning_effort: str | None = None,
    temperature: float | None = EVAL_TEMPERATURE,
    cache: EvalCache | None = None,
    refresh_cache: bool = False,
):
    selected = list(cases)
    if len(selected) <= 1:
        return [
            run_case(
                case,
                model=model,
                max_tool_iterations=max_tool_iterations,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                cache=cache,
                refresh_cache=refresh_cache,
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
                    reasoning_effort=reasoning_effort,
                    temperature=temperature,
                    cache=cache,
                    refresh_cache=refresh_cache,
                )
            )
            progress.advance(task_id)
    return results


def _normalize_reasoning_effort(value: str) -> str | None:
    """Normalize CLI reasoning effort to the llm.call_llm convention."""
    return None if value == "none" else value


if __name__ == "__main__":
    main()
