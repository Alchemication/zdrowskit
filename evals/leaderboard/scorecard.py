"""Feature-first views over recorded eval runs.

The leaderboard answers two questions, in this order. First: does the code we
ship pass its evals right now? That is the production scorecard — one row per
feature, taken from the newest run that resolved production routes, scored
against the cases that exist today. Second: would some other model or setting
do better? That is the per-feature variation table.

Grouping is by feature, not by case set. A feature maps to one production route
in `model_prefs`, matrix runs are already feature-scoped, and model decisions
are only meaningful within a feature. Keying on the case-set hash instead made
every new eval case start a fresh section with no history to compare against.
"""

from __future__ import annotations

from typing import Any

from evals.framework import EvalCase, load_cases
from evals.leaderboard.record import route_label

FEATURE_BLURBS = {
    "chat": "Answering your questions in Telegram, including the SQL it writes.",
    "insights": "The weekly report.",
    "nudge": "Short, timely messages during the day.",
    "memory": "What carries over from one week to the next.",
    "verification_judge": "The second model that fact-checks a draft before it is sent.",
}
"""Plain-language description of each feature, for readers outside the codebase.

The leaderboard is published, so `verification_judge` has to mean something to
someone who has never seen `model_prefs`. A feature with no entry here renders
without a description rather than with a placeholder.
"""


def build_scorecard(
    runs: list[dict[str, Any]],
    *,
    inventory: list[EvalCase] | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Build the production scorecard and per-feature variation tables.

    Args:
        runs: All persisted run records.
        inventory: Cases on disk today. Defaults to loading `evals/cases`.
        head_sha: Current commit, recorded in the payload for display.

    Returns:
        A payload shared by the Markdown and HTML renderers.
    """
    cases = load_cases() if inventory is None else inventory
    by_feature = _inventory_by_feature(cases)
    production = [
        _production_row(feature, case_ids, runs)
        for feature, case_ids in sorted(by_feature.items())
    ]
    features = [
        _feature_section(feature, case_ids, runs)
        for feature, case_ids in sorted(by_feature.items())
    ]
    return {
        "head_sha": head_sha,
        "total_cases": len(cases),
        "production": production,
        "features": [section for section in features if section["rows"]],
        "uncovered_features": [
            row["feature"] for row in production if row["row"] is None
        ],
    }


def _inventory_by_feature(cases: list[EvalCase]) -> dict[str, list[str]]:
    """Map each feature to the case ids that exist for it today."""
    by_feature: dict[str, list[str]] = {}
    for case in cases:
        by_feature.setdefault(case.feature, []).append(case.id)
    return {feature: sorted(ids) for feature, ids in by_feature.items()}


def _production_row(
    feature: str,
    inventory_case_ids: list[str],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the production scorecard row for one feature."""
    candidates = [
        run
        for run in _newest_first(runs)
        if run.get("is_production") and _feature_rows(run, feature)
    ]
    if not candidates:
        return {
            "feature": feature,
            "blurb": FEATURE_BLURBS.get(feature, ""),
            "total_cases": len(inventory_case_ids),
            "row": None,
            "missing_case_ids": inventory_case_ids,
        }
    run = candidates[0]
    row = _variation_row(run, feature)
    covered = set(row["case_ids"])
    row["missing_case_ids"] = [
        case_id for case_id in inventory_case_ids if case_id not in covered
    ]
    return {
        "feature": feature,
        "blurb": FEATURE_BLURBS.get(feature, ""),
        "total_cases": len(inventory_case_ids),
        "row": row,
        "missing_case_ids": row["missing_case_ids"],
    }


def _feature_section(
    feature: str,
    inventory_case_ids: list[str],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the variation table for one feature."""
    latest_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for run in _newest_first(runs):
        if not _feature_rows(run, feature):
            continue
        identity = (
            run.get("requested_model"),
            run.get("reasoning_effort"),
            run.get("route_set_id"),
            int(run.get("repeat", 1)),
        )
        latest_by_identity.setdefault(identity, run)
    rows = [_variation_row(run, feature) for run in latest_by_identity.values()]
    rows.sort(
        key=lambda row: (
            -float(row["strict_accuracy"]),
            -float(row["accuracy"]),
            int(row["flaky_count"]),
            _sort_optional_number(row["cost_per_repeat"]),
            _sort_optional_number(row["avg_latency_s"]),
        )
    )
    return {
        "feature": feature,
        "blurb": FEATURE_BLURBS.get(feature, ""),
        "total_cases": len(inventory_case_ids),
        "case_ids": inventory_case_ids,
        "rows": rows,
    }


def _variation_row(run: dict[str, Any], feature: str) -> dict[str, Any]:
    """Build one comparable row from a run's slice of a feature."""
    rows = _feature_rows(run, feature)
    metrics = slice_metrics(rows, repeat=int(run.get("repeat", 1)))
    git_sha = str(run.get("git_sha", "unknown"))
    return {
        "run_id": run["run_id"],
        "feature": feature,
        "created_at": run["created_at"],
        "requested_model": run.get("requested_model"),
        "model_display": format_requested_model(run.get("requested_model")),
        "is_production": bool(run.get("is_production")),
        "reasoning_effort": run.get("reasoning_effort"),
        "repeat": int(run.get("repeat", 1)),
        "route_set_id": run.get("route_set_id"),
        "routes": sorted(
            {route_label(row["route"] or {"primary": row["model"]}) for row in rows}
        ),
        "case_ids": [str(row["case_id"]) for row in rows],
        "git_sha": git_sha,
        "git_sha_short": git_sha[:7] if git_sha != "unknown" else git_sha,
        "dirty": bool(run.get("dirty", False)),
        "revision_label": format_revision(run),
        "recorded_on": str(run.get("created_at", ""))[:10],
        "case_rows": [_case_row(row) for row in rows],
        "fallback_case_ids": [
            str(row["case_id"]) for row in rows if row.get("fallback_used")
        ],
        "flaky_case_ids": [str(row["case_id"]) for row in rows if row["flaky"]],
        "failed_case_ids": [
            str(row["case_id"]) for row in rows if row["outcome"] == "fail"
        ],
        "errored_case_ids": [
            str(row["case_id"]) for row in rows if row["outcome"] == "errored"
        ],
        **metrics,
    }


def _case_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build one per-case cell for a variation row."""
    return {
        "case_id": str(row["case_id"]),
        "outcome": str(row["outcome"]),
        "passes": int(row["passes"]),
        "scored": int(row["scored"]),
        "runs": int(row["runs"]),
        "errored": int(row["errored"]),
        "pass_rate": row["pass_rate"],
        "flaky": bool(row["flaky"]),
        "rate_label": f"{row['passes']}/{row['scored']}"
        if row["scored"]
        else "errored",
        "failure_names": list(row["failure_names"]),
    }


def slice_metrics(rows: list[dict[str, Any]], *, repeat: int) -> dict[str, Any]:
    """Aggregate metrics over a set of per-case rows.

    Attempt-weighted `accuracy` estimates a single run's score; strict accuracy
    counts only cases that passed every attempt. Errored attempts stay out of
    both denominators, so a provider outage cannot read as a quality drop.
    """
    scored_rows = [row for row in rows if row["scored"]]
    scored = sum(int(row["scored"]) for row in rows)
    passed = sum(int(row["passes"]) for row in rows)
    stable_pass = sum(1 for row in scored_rows if row["outcome"] == "pass")
    latencies = [
        attempt["latency_s"]
        for row in rows
        for attempt in row["attempts"]
        if attempt["latency_s"] is not None
    ]
    costs = [
        attempt["cost"]
        for row in rows
        for attempt in row["attempts"]
        if attempt["cost"] is not None
    ]
    total_cost = sum(costs) if costs else None
    return {
        "case_count": len(rows),
        # The denominators behind the two percentages. A published percentage
        # that cannot be read back as "N of M" is a number the reader has to
        # take on trust.
        "scored_case_count": len(scored_rows),
        "scored_attempts": scored,
        "accuracy": (passed / scored * 100.0) if scored else 0.0,
        "strict_accuracy": (
            (stable_pass / len(scored_rows) * 100.0) if scored_rows else 0.0
        ),
        "passed": passed,
        "failed": scored - passed,
        "errored": sum(int(row["errored"]) for row in rows),
        "attempts": sum(int(row["runs"]) for row in rows),
        "flaky_count": sum(1 for row in rows if row["flaky"]),
        "stable_pass_count": stable_pass,
        "stable_fail_count": sum(1 for row in scored_rows if row["outcome"] == "fail"),
        "avg_latency_s": (sum(latencies) / len(latencies)) if latencies else None,
        "total_cost": total_cost,
        "cost_per_repeat": (total_cost / repeat) if total_cost is not None else None,
    }


def _feature_rows(run: dict[str, Any], feature: str) -> list[dict[str, Any]]:
    """Return a run's aggregated case rows for one feature."""
    return [row for row in run.get("per_case", []) if row.get("feature") == feature]


def _newest_first(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort runs newest first by recorded timestamp."""
    return sorted(runs, key=lambda run: str(run.get("created_at", "")), reverse=True)


def format_requested_model(requested_model: str | None) -> str:
    """Render the requested model, or say the shipped route answered.

    A run without an explicit --model asks each feature's own configured
    route, so there is no single model to name. The row's `routes` field names
    the model that actually answered.
    """
    if not requested_model:
        return "Shipped route"
    return requested_model.split("/")[-1]


def format_revision(run: dict[str, Any]) -> str:
    """Format a compact git revision label for leaderboard display."""
    sha = str(run.get("git_sha", "unknown"))
    short_sha = sha[:7] if sha != "unknown" else sha
    return f"{short_sha}*" if bool(run.get("dirty", False)) else short_sha


def display_reasoning_effort(value: str | None) -> str:
    """Render a stored reasoning effort for leaderboard display.

    A production run records the literal string "production" here, because no
    single effort was requested — each feature used its own. Naming it as a
    reasoning level reads as one, so it is spelled out instead.
    """
    if value == "production":
        return "as configured"
    return value or "none"


def _sort_optional_number(value: Any) -> float:
    """Return a sortable numeric value, pushing missing values last."""
    if value is None:
        return float("inf")
    return float(value)
