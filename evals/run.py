"""Run feedback-derived evals.

Usage:
    uv run python -m evals.run
    uv run python -m evals.run chat_log_life_disruption
    uv run python -m evals.run --feature chat
    uv run python -m evals.run --feature insights
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from evals import leaderboard
from evals.framework import (
    EVAL_MAX_CONCURRENCY,
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
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "litellm model string. Default: whatever each case's feature "
            "routes to in production, so a green suite means the models you "
            "actually ship are green."
        ),
    )
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
        choices=["production", "none", "low", "medium", "high"],
        default="production",
        help="Reasoning effort for eval calls. Default: the production route's.",
    )
    parser.add_argument(
        "--no-temperature",
        action="store_true",
        help=(
            "Omit the temperature parameter from LLM calls. Required for "
            "models that reject it (e.g. claude-opus-5)."
        ),
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help=(
            "Reuse cached eval LLM responses. Off by default: a cached "
            "response is one frozen sample, which hides the run-to-run "
            "variation these evals exist to measure. Use it while iterating "
            "on assertions, never to judge a model."
        ),
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="With --cache, ignore cached responses and overwrite them.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Run each case N times and report a per-case pass rate. Model "
            "output varies between identical runs, so a single run is one "
            "sample, not a verdict. Always uncached."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "Eval executions to run in parallel. Defaults to --repeat, so "
            "repeats of a case overlap instead of running back to back. Raise "
            "it to also overlap different cases; capped at "
            f"{EVAL_MAX_CONCURRENCY}."
        ),
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
    if args.refresh_cache and not args.cache:
        parser.error("--refresh-cache requires --cache")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.repeat > 1 and args.cache:
        parser.error(
            "--repeat cannot be used with --cache: repeated runs would replay "
            "one cached response and report it as stability."
        )
    if args.record_duplicate and not args.record:
        parser.error("--record-duplicate requires --record")
    concurrency = args.repeat if args.concurrency is None else args.concurrency
    if concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if concurrency > 1 and args.cache:
        parser.error(
            "--concurrency cannot be used with --cache: the response cache is "
            "a single SQLite connection and is not safe to share across threads."
        )

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
    cache = EvalCache() if args.cache else None
    results = _run_selected_cases(
        selected,
        model=args.model,
        max_tool_iterations=args.max_tool_iterations,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        cache=cache,
        refresh_cache=args.refresh_cache,
        repeat=args.repeat,
        concurrency=concurrency,
    )
    print_results(results)
    if args.details:
        print_result_details(results)
    if args.record:
        outcome = leaderboard.record_run(
            results=results,
            case_ids=[case.id for case in selected],
            requested_model=args.model,
            reasoning_effort=reasoning_effort,
            max_tool_iterations=args.max_tool_iterations,
            feature_filter=args.feature,
            repeat=args.repeat,
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
    model: str | None,
    max_tool_iterations: int,
    reasoning_effort: str | None = None,
    temperature: float | None = EVAL_TEMPERATURE,
    cache: EvalCache | None = None,
    refresh_cache: bool = False,
    repeat: int = 1,
    concurrency: int = 1,
):
    """Run every selected case, `repeat` times each.

    Results come back in submission order — all attempts of the first case,
    then the second — regardless of the order they finish in, so the printed
    table and the recorded aggregation do not shuffle between runs.
    """
    selected = [case for case in cases for _ in range(max(repeat, 1))]

    def _run(case: EvalCase):
        return run_case(
            case,
            model=model,
            max_tool_iterations=max_tool_iterations,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            cache=cache,
            refresh_cache=refresh_cache,
        )

    if len(selected) <= 1:
        return [_run(case) for case in selected]

    workers = max(1, min(concurrency, EVAL_MAX_CONCURRENCY, len(selected)))
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        label = "Running feedback evals"
        if workers > 1:
            label += f" ({workers} in parallel)"
        task_id = progress.add_task(label, total=len(selected))
        if workers == 1:
            results = []
            for case in selected:
                progress.update(task_id, description=f"[bold]{case.id}[/bold]")
                results.append(_run(case))
                progress.advance(task_id)
            return results

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_run, case): index
                for index, case in enumerate(selected)
            }
            ordered: list[Any] = [None] * len(selected)
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
                progress.advance(task_id)
    return ordered


def _normalize_reasoning_effort(value: str) -> str | None:
    """Normalize CLI reasoning effort to the llm.call_llm convention.

    None means reasoning off. The "production" sentinel passes through so
    run_case can inherit the route's own effort — distinct from "none", which
    explicitly disables reasoning for a feature production may run with it on.
    """
    return None if value == "none" else value


if __name__ == "__main__":
    main()
