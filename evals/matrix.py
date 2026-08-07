"""Run eval comparison matrices.

Usage:
    uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high
    uv run python -m evals.matrix --feature insights --models anthropic/claude-opus-5,deepseek/deepseek-v4-pro --reasoning-efforts high
    uv run python -m evals.matrix --production --record
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from evals import leaderboard
from evals.framework import (
    PRODUCTION_EFFORT,
    EVAL_TEMPERATURE,
    EvalCache,
    EvalCase,
    EvalResult,
    load_cases,
    print_results,
)
from evals.run import _normalize_reasoning_effort, _run_selected_cases, select_cases

DIRECT_MODEL_FEATURES = {"chat", "nudge", "insights", "memory", "verification_judge"}


@dataclass(frozen=True)
class MatrixRun:
    """One eval matrix cell."""

    label: str
    cases: list[EvalCase]
    model: str | None
    """Model override, or None to use each case's own production route."""
    reasoning_effort: str | None
    temperature: float | None


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Run eval model/route matrices.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--feature",
        help="Feature slice to compare. Use chat for model comparisons.",
    )
    mode.add_argument(
        "--production",
        action="store_true",
        help="Run the current production smoke suite once.",
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="Optional case ids. Applies after --feature filtering.",
    )
    parser.add_argument(
        "--models",
        help="Comma-separated litellm model strings for direct LLM feature matrices.",
    )
    parser.add_argument(
        "--reasoning-efforts",
        default="none",
        help="Comma-separated reasoning efforts: none,low,medium,high.",
    )
    parser.add_argument(
        "--max-tool-iterations",
        type=int,
        default=5,
        help="Maximum chat tool loop iterations before final synthesis.",
    )
    parser.add_argument(
        "--no-temperature",
        action="store_true",
        help="Omit temperature from LLM calls.",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help=(
            "Reuse cached eval LLM responses. Off by default: comparing "
            "models from frozen samples measures the cache, not the models."
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
            "Run each case N times per cell and report a per-case pass rate. "
            "Strongly recommended for model comparisons: output varies "
            "between identical runs, so one sample per cell is noise."
        ),
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Record every matrix cell to the leaderboard history.",
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

    try:
        cases = load_cases()
        runs = build_matrix_runs(
            cases,
            feature=args.feature,
            production=args.production,
            case_ids=args.cases or None,
            models=_split_csv(args.models),
            reasoning_efforts=_split_csv(args.reasoning_efforts) or ["none"],
            temperature=None if args.no_temperature else EVAL_TEMPERATURE,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    cache = EvalCache() if args.cache else None
    failures = 0
    for matrix_run in runs:
        print(f"\n== {matrix_run.label} ==")
        results = _run_selected_cases(
            matrix_run.cases,
            model=matrix_run.model,
            max_tool_iterations=args.max_tool_iterations,
            reasoning_effort=matrix_run.reasoning_effort,
            temperature=matrix_run.temperature,
            cache=cache,
            repeat=args.repeat,
            refresh_cache=args.refresh_cache,
        )
        print_results(results)
        if args.record:
            _record_matrix_run(
                results,
                matrix_run=matrix_run,
                max_tool_iterations=args.max_tool_iterations,
                feature_filter=args.feature,
                allow_duplicate=args.record_duplicate,
            )
        if not all(result.passed for result in results):
            failures += 1
    if failures:
        sys.exit(1)


def build_matrix_runs(
    cases: list[EvalCase],
    *,
    feature: str | None,
    production: bool,
    case_ids: list[str] | None,
    models: list[str],
    reasoning_efforts: list[str],
    temperature: float | None,
) -> list[MatrixRun]:
    """Build matrix cells from CLI options."""
    if production:
        if models:
            raise ValueError("--production uses current routes; do not pass --models")
        selected = select_cases(cases, case_ids=case_ids)
        return [
            MatrixRun(
                label=f"production · {len(selected)} cases",
                cases=selected,
                # None resolves each case against model_prefs, so this cell
                # asks exactly what the daemon asks. Naming one model here
                # made the smoke suite green for a config nobody ships.
                model=None,
                reasoning_effort=PRODUCTION_EFFORT,
                temperature=temperature,
            )
        ]

    if not feature:
        raise ValueError("Pass --feature for a matrix, or --production for smoke.")
    selected = select_cases(cases, case_ids=case_ids, feature=feature)
    if feature in DIRECT_MODEL_FEATURES:
        if not models:
            raise ValueError(f"--feature {feature} requires --models")
        return [
            MatrixRun(
                label=(
                    f"{feature} · {model} · reasoning="
                    f"{_display_reasoning_effort(effort)}"
                ),
                cases=selected,
                model=model,
                reasoning_effort=_normalize_reasoning_effort(effort),
                temperature=temperature,
            )
            for model in models
            for effort in reasoning_efforts
        ]

    raise ValueError(f"Unsupported matrix feature: {feature}")


def _record_matrix_run(
    results: list[EvalResult],
    *,
    matrix_run: MatrixRun,
    max_tool_iterations: int,
    feature_filter: str | None,
    allow_duplicate: bool,
) -> None:
    outcome = leaderboard.record_run(
        results=results,
        case_ids=[case.id for case in matrix_run.cases],
        requested_model=matrix_run.model,
        reasoning_effort=matrix_run.reasoning_effort,
        max_tool_iterations=max_tool_iterations,
        feature_filter=feature_filter,
        allow_duplicate=allow_duplicate,
    )
    if outcome.recorded:
        print(f"Recorded leaderboard run {outcome.record['run_id']}")
    else:
        print(f"Matching leaderboard run already recorded ({outcome.record['run_id']})")


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated CLI value into non-empty parts."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _display_reasoning_effort(value: str | None) -> str:
    """Render a CLI reasoning effort value."""
    return "none" if _normalize_reasoning_effort(value or "none") is None else value


if __name__ == "__main__":
    main()
