"""Named scenarios that perturb blueprint data for eval testing.

Each scenario is a function that takes (context, health_data) and returns
a modified (context, health_data) tuple.  The caller is responsible for
passing deep copies — scenarios may mutate in place.

Scenarios:
    baseline       — Unmodified real data.  The control case.
    rest_day       — Last day has no workouts.  Tests missed_session SKIP logic.
    no_runs_week   — All run workouts removed, only lifts remain.
                     Tests report handling when there's no pace data.
"""

from __future__ import annotations

_RUN_SUMMARY_FIELDS = {
    "run_count",
    "total_run_km",
    "avg_run_km",
    "best_pace_min_per_km",
    "avg_run_hr",
    "peak_run_hr",
    "avg_elevation_gain_m",
    "avg_running_power_w",
    "avg_running_stride_m",
    "avg_run_temp_c",
    "avg_run_humidity_pct",
    "run_consistency_pct",
}


def baseline(context: dict[str, str], health_data: dict) -> tuple[dict[str, str], dict]:
    """Unmodified real data — the control case."""
    return context, health_data


def rest_day(context: dict[str, str], health_data: dict) -> tuple[dict[str, str], dict]:
    """Last day in the week has no workouts.

    Removes the ``workouts`` list from the final day in
    ``current_week.days``.  Useful with ``trigger_type="missed_session"``
    to verify the LLM produces SKIP on a rest day.
    """
    days = health_data.get("current_week", {}).get("days", [])
    if days:
        last = days[-1]
        last.pop("workouts", None)
    return context, health_data


def no_runs_week(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Remove all run workouts — only strength sessions remain.

    Strips run-category workouts from every day and zeroes out
    run-related fields in the week summary.  Tests that the report
    handles missing pace/distance data gracefully.
    """
    # Strip run workouts from each day.
    for day in health_data.get("current_week", {}).get("days", []):
        if "workouts" in day:
            day["workouts"] = [w for w in day["workouts"] if w.get("category") != "run"]

    # Zero out run summary fields.
    summary = health_data.get("current_week", {}).get("summary")
    if summary:
        for field in _RUN_SUMMARY_FIELDS:
            if field in summary:
                summary[field] = 0 if "count" in field or "pct" in field else None

    return context, health_data


# Registry for the runner's --scenario filter.
ALL_SCENARIOS: dict[str, callable] = {
    "baseline": baseline,
    "rest_day": rest_day,
    "no_runs_week": no_runs_week,
}
