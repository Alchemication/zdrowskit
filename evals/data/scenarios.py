"""Named scenarios that perturb blueprint data for eval testing.

Each scenario is a function that takes (context, health_data) and returns
a modified (context, health_data) tuple.  The caller is responsible for
passing deep copies — scenarios may mutate in place.
"""

from __future__ import annotations

# Sleep field keys to clear when setting a sleep marker.
_SLEEP_FIELDS = [
    "sleep_total_h",
    "sleep_in_bed_h",
    "sleep_efficiency_pct",
    "sleep_deep_h",
    "sleep_core_h",
    "sleep_rem_h",
    "sleep_awake_h",
]


def baseline(context: dict[str, str], health_data: dict) -> tuple[dict[str, str], dict]:
    """Unmodified real data — the control case."""
    return context, health_data


def rest_day(context: dict[str, str], health_data: dict) -> tuple[dict[str, str], dict]:
    """Last day has no workouts.

    Useful with ``trigger_type="missed_session"`` to verify the LLM
    produces SKIP on a rest day.
    """
    days = health_data.get("current_week", {}).get("days", [])
    if days:
        days[-1].pop("workouts", None)
    return context, health_data


def training_day_missed(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """A training day (weekday) with no workout — should trigger a nudge.

    Removes workouts from the last weekday that had one, making it look
    like a missed session on a day the plan expected activity.
    """
    days = health_data.get("current_week", {}).get("days", [])
    # Find last day with workouts and clear them.
    for day in reversed(days):
        if day.get("workouts"):
            day["workouts"] = []
            break
    return context, health_data


def boring_new_data(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Last day is completely unremarkable — nothing new to comment on.

    Sets the last day's metrics to near-average values with no workouts,
    no outliers, nothing interesting.
    """
    days = health_data.get("current_week", {}).get("days", [])
    if not days:
        return context, health_data

    summary = health_data.get("current_week", {}).get("summary", {})
    last = days[-1]

    # Make it a boring rest day with average metrics.
    last["workouts"] = []
    last["steps"] = int(summary.get("avg_steps", 6000))
    last["resting_hr"] = int(summary.get("avg_resting_hr", 53))
    last["hrv_ms"] = summary.get("avg_hrv_ms", 49.0)
    last["recovery_index"] = summary.get("avg_recovery_index", 0.94)

    return context, health_data


def redundant_nudge(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """No data change — the recent_nudges config handles redundancy.

    The caller sets ``config["recent_nudges"]`` to a nudge that already
    covers the notable observation in the data.
    """
    return context, health_data


def log_update_meaningful(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Append a meaningful log entry the LLM should respond to."""
    entry = (
        "\n\n## 2026-03-28\n"
        "Back feeling stiff after yesterday's deadlifts. "
        "Not painful but definitely tight in the lower back."
    )
    context["log"] = context.get("log", "") + entry
    return context, health_data


# ---------------------------------------------------------------------------
# Sleep marker scenarios
# ---------------------------------------------------------------------------


def _set_sleep_marker(day: dict, marker: str) -> None:
    """Replace a day's sleep data with a string marker."""
    for field in _SLEEP_FIELDS:
        day.pop(field, None)
    day["sleep"] = marker


def sleep_last_night_query(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Set up a clear "last night" sleep scenario for chat queries.

    Injects realistic sleep data into yesterday (second-to-last day) and
    strips all sleep fields from today (last day) with no marker — matching
    the production behavior where today has no sleep because tonight hasn't
    happened yet.
    """
    days = health_data.get("current_week", {}).get("days", [])
    if len(days) < 2:
        return context, health_data

    # Yesterday: inject known sleep data.
    yesterday = days[-2]
    yesterday["sleep_total_h"] = 7.63
    yesterday["sleep_in_bed_h"] = 8.31
    yesterday["sleep_efficiency_pct"] = 91.9
    yesterday["sleep_deep_h"] = 0.87
    yesterday["sleep_core_h"] = 5.48
    yesterday["sleep_rem_h"] = 1.28
    yesterday["sleep_awake_h"] = 0.68

    # Today: no sleep fields at all (tonight hasn't happened).
    today = days[-1]
    for field in _SLEEP_FIELDS:
        today.pop(field, None)
    today.pop("sleep", None)

    return context, health_data


def sleep_sync_pending_yesterday(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Yesterday's sleep set to 'sync_pending'."""
    days = health_data.get("current_week", {}).get("days", [])
    if len(days) >= 2:
        _set_sleep_marker(days[-2], "sync_pending")
    return context, health_data


def sleep_not_tracked_single(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Exactly one past day has 'not_tracked' — below the 3-day threshold."""
    days = health_data.get("current_week", {}).get("days", [])
    # Pick a middle day (index 2) so it's clearly a single occurrence.
    if len(days) >= 4:
        _set_sleep_marker(days[2], "not_tracked")
    return context, health_data


def sleep_not_tracked_3_consecutive(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Three consecutive past days have 'not_tracked'."""
    days = health_data.get("current_week", {}).get("days", [])
    # Set days at indices 2, 3, 4 (Wed, Thu, Fri) to not_tracked.
    for i in range(2, min(5, len(days))):
        _set_sleep_marker(days[i], "not_tracked")
    return context, health_data


# ---------------------------------------------------------------------------
# Mid-week awareness scenarios
# ---------------------------------------------------------------------------


def midweek_wednesday(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Truncate to Mon-Wed only (3 days) to simulate a mid-week check.

    After truncation the user has 1 run and 1 lift — well behind the
    plan's 3 runs + 2 lifts/week target, but the week isn't over.
    """
    days = health_data.get("current_week", {}).get("days", [])
    # Keep only the first 3 days.
    health_data["current_week"]["days"] = days[:3]

    # Recalculate summary to match truncated data.
    kept = health_data["current_week"]["days"]
    summary = health_data.get("current_week", {}).get("summary", {})

    runs = [
        w for d in kept for w in d.get("workouts", []) if w.get("category") == "run"
    ]
    lifts = [
        w for d in kept for w in d.get("workouts", []) if w.get("category") == "lift"
    ]

    summary["run_count"] = len(runs)
    summary["lift_count"] = len(lifts)
    if runs:
        summary["total_run_km"] = sum(w.get("gpx_distance_km", 0) or 0 for w in runs)
        summary["avg_run_km"] = summary["total_run_km"] / len(runs)
    else:
        summary["total_run_km"] = 0
        summary["avg_run_km"] = 0.0

    n = len(kept)
    if n:
        summary["avg_resting_hr"] = round(
            sum(d.get("resting_hr", 0) or 0 for d in kept) / n, 1
        )
        hrv_vals = [d["hrv_ms"] for d in kept if d.get("hrv_ms") is not None]
        summary["avg_hrv_ms"] = (
            round(sum(hrv_vals) / len(hrv_vals), 1) if hrv_vals else None
        )

    return context, health_data


# ---------------------------------------------------------------------------
# Recovery verdict scenarios
# ---------------------------------------------------------------------------


def recovery_crashed(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Simulate poor recovery: HRV crashed, bad sleep, elevated resting HR.

    Makes the signal unambiguous — the LLM should say "back off".
    """
    days = health_data.get("current_week", {}).get("days", [])
    summary = health_data.get("current_week", {}).get("summary", {})

    for day in days:
        day["hrv_ms"] = 28.0
        day["resting_hr"] = 62
        day["recovery_index"] = 0.45

        # Bad sleep on days that have sleep data.
        if day.get("sleep") not in ("pending", "sync_pending", "not_tracked"):
            day["sleep_total_h"] = 4.8
            day["sleep_efficiency_pct"] = 68.0
            day["sleep_deep_h"] = 0.2
            day["sleep_rem_h"] = 0.8
            day["sleep_awake_h"] = 1.5

    summary["avg_hrv_ms"] = 28.0
    summary["avg_resting_hr"] = 62
    summary["avg_recovery_index"] = 0.45
    summary["hrv_trend"] = "declining"
    summary["avg_sleep_total_h"] = 4.8
    summary["avg_sleep_efficiency_pct"] = 68.0
    summary["avg_sleep_deep_h"] = 0.2
    summary["avg_sleep_rem_h"] = 0.8

    return context, health_data


def recovery_green(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Simulate excellent recovery: HRV high, great sleep, low resting HR.

    The LLM should say "push" or "ready to progress".
    """
    days = health_data.get("current_week", {}).get("days", [])
    summary = health_data.get("current_week", {}).get("summary", {})

    for day in days:
        day["hrv_ms"] = 62.0
        day["resting_hr"] = 48
        day["recovery_index"] = 1.29

        if day.get("sleep") not in ("pending", "sync_pending", "not_tracked"):
            day["sleep_total_h"] = 7.8
            day["sleep_efficiency_pct"] = 97.0
            day["sleep_deep_h"] = 1.1
            day["sleep_rem_h"] = 2.2
            day["sleep_awake_h"] = 0.15

    summary["avg_hrv_ms"] = 62.0
    summary["avg_resting_hr"] = 48
    summary["avg_recovery_index"] = 1.29
    summary["hrv_trend"] = "improving"
    summary["avg_sleep_total_h"] = 7.8
    summary["avg_sleep_efficiency_pct"] = 97.0
    summary["avg_sleep_deep_h"] = 1.1
    summary["avg_sleep_rem_h"] = 2.2

    return context, health_data


def recovery_mixed(
    context: dict[str, str], health_data: dict
) -> tuple[dict[str, str], dict]:
    """Simulate mixed signals: HRV slightly down, sleep OK, resting HR normal.

    The LLM should suggest maintaining current load.
    """
    days = health_data.get("current_week", {}).get("days", [])
    summary = health_data.get("current_week", {}).get("summary", {})

    for day in days:
        day["hrv_ms"] = 42.0
        day["resting_hr"] = 54
        day["recovery_index"] = 0.78

        if day.get("sleep") not in ("pending", "sync_pending", "not_tracked"):
            day["sleep_total_h"] = 7.2
            day["sleep_efficiency_pct"] = 95.0
            day["sleep_deep_h"] = 0.8
            day["sleep_rem_h"] = 1.9
            day["sleep_awake_h"] = 0.3

    summary["avg_hrv_ms"] = 42.0
    summary["avg_resting_hr"] = 54
    summary["avg_recovery_index"] = 0.78
    summary["hrv_trend"] = "stable"
    summary["avg_sleep_total_h"] = 7.2
    summary["avg_sleep_efficiency_pct"] = 95.0
    summary["avg_sleep_deep_h"] = 0.8
    summary["avg_sleep_rem_h"] = 1.9

    return context, health_data


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_SCENARIOS: dict[str, callable] = {
    "baseline": baseline,
    "rest_day": rest_day,
    "training_day_missed": training_day_missed,
    "boring_new_data": boring_new_data,
    "redundant_nudge": redundant_nudge,
    "log_update_meaningful": log_update_meaningful,
    "sleep_last_night_query": sleep_last_night_query,
    "sleep_sync_pending_yesterday": sleep_sync_pending_yesterday,
    "sleep_not_tracked_single": sleep_not_tracked_single,
    "sleep_not_tracked_3_consecutive": sleep_not_tracked_3_consecutive,
    "midweek_wednesday": midweek_wednesday,
    "recovery_crashed": recovery_crashed,
    "recovery_green": recovery_green,
    "recovery_mixed": recovery_mixed,
}
