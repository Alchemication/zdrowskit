"""Run zdrowskit AI evals against one or more models.

Each eval case is defined in ``evals/data/cases.json`` — a human-labelled
dataset of scenarios with expected behaviors.  The runner loads each case,
applies its scenario perturbation, calls the LLM, and checks assertions.

Usage:
    uv run python -m evals.run                          # all cases, default model
    uv run python -m evals.run --suite core             # only product-gating cases
    uv run python -m evals.run --no-cache               # bypass eval cache
    uv run python -m evals.run nudge_rest_day_skip      # one case by ID
    uv run python -m evals.run --category sleep_markers  # all cases in a category

Model selection (full litellm string, comma-separated for comparison):
    uv run python -m evals.run --model anthropic/claude-sonnet-4-6
    uv run python -m evals.run --model anthropic/claude-sonnet-4-6,anthropic/claude-haiku-4-5-20251001

Reasoning effort (forwarded to call_llm):
    uv run python -m evals.run --reasoning-effort low

Other tools:
    uv run python -m evals.data.extract                 # refresh pinned blueprints
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is importable.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from evals.framework import (  # noqa: E402
    DEFAULT_MODEL,
    EvalResult,
    load_cases,
    print_results,
    run_case,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run zdrowskit AI evals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="Case IDs to run (default: all).",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Run only cases in this category.",
    )
    parser.add_argument(
        "--suite",
        default=None,
        choices=["core", "benchmark"],
        help="Run only cases in this suite.",
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
        "--reasoning-effort",
        default=None,
        help="Reasoning effort hint forwarded to the LLM.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run at most N cases (useful for quick harness sanity checks).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the shared eval cache for this run.",
    )
    args = parser.parse_args()

    all_cases = load_cases()

    # Filter cases.
    if args.cases:
        ids = set(args.cases)
        selected = [c for c in all_cases if c["id"] in ids]
        unknown = ids - {c["id"] for c in selected}
        if unknown:
            all_ids = [c["id"] for c in all_cases]
            print(f"Unknown case(s): {unknown}. Available: {all_ids}", file=sys.stderr)
            sys.exit(1)
    elif args.category:
        selected = [c for c in all_cases if c["category"] == args.category]
        if not selected:
            categories = sorted({c["category"] for c in all_cases})
            print(
                f"No cases in category '{args.category}'. Available: {categories}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        selected = all_cases

    if args.suite:
        selected = [c for c in selected if c["suite"] == args.suite]
        if not selected:
            print(
                f"No cases in suite '{args.suite}'.",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.limit is not None:
        selected = selected[: args.limit]

    models = [m.strip() for m in args.model.split(",")]
    total = len(selected) * len(models)

    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    results: list[EvalResult] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task_id = progress.add_task("Starting...", total=total)

        for model in models:
            for case in selected:
                result = run_case(
                    case=case,
                    model=model,
                    reasoning_effort=args.reasoning_effort,
                    progress=progress,
                    task_id=task_id,
                    use_cache=not args.no_cache,
                )
                results.append(result)

    print()
    print_results(results)

    if not all(r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
