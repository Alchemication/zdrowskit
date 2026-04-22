"""Lifetime and PR milestone summaries for LLM prompts."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta


def _format_pace(value: float | None) -> str | None:
    """Format minutes/km as mm:ss/km."""
    if value is None:
        return None
    total_seconds = int(round(value * 60))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}/km"


def _format_age_days(date_str: str) -> str:
    """Return a human-readable age for a milestone date."""
    days_old = (date.today() - date.fromisoformat(date_str)).days
    if days_old == 0:
        return "today"
    if days_old == 1:
        return "1 day ago"
    return f"{days_old} days ago"


def _best_run_window(conn: sqlite3.Connection, km_count: int) -> sqlite3.Row | None:
    """Return the best contiguous run window of the requested distance."""
    return conn.execute(
        f"""
        WITH split_windows AS (
            SELECT
                w.date,
                ws.start_utc,
                ws.km_index,
                COUNT(*) OVER (
                    PARTITION BY ws.start_utc
                    ORDER BY ws.km_index
                    ROWS BETWEEN CURRENT ROW AND {km_count - 1} FOLLOWING
                ) AS split_count,
                SUM(ws.pace_min_km) OVER (
                    PARTITION BY ws.start_utc
                    ORDER BY ws.km_index
                    ROWS BETWEEN CURRENT ROW AND {km_count - 1} FOLLOWING
                ) AS total_pace_min
            FROM workout_split AS ws
            JOIN workout AS w
              ON w.start_utc = ws.start_utc
            WHERE w.category = 'run'
        )
        SELECT
            date,
            start_utc,
            km_index,
            total_pace_min / {km_count}.0 AS pace_min_km
        FROM split_windows
        WHERE split_count = {km_count}
        ORDER BY total_pace_min ASC, date ASC, km_index ASC
        LIMIT 1
        """
    ).fetchone()


def _week_start(date_str: str) -> date:
    """Return the Monday date for the ISO week containing date_str."""
    day = date.fromisoformat(date_str)
    return day - timedelta(days=day.weekday())


def _longest_weekly_streak(
    conn: sqlite3.Connection,
    where_clause: str,
    params: tuple[object, ...] = (),
) -> tuple[int, str, str] | None:
    """Return the longest streak of consecutive weeks matching a query."""
    rows = conn.execute(
        f"""
        SELECT DISTINCT date
        FROM workout_all
        WHERE {where_clause}
        ORDER BY date ASC
        """,
        params,
    ).fetchall()
    if not rows:
        return None

    weeks = sorted({_week_start(row["date"]) for row in rows})
    longest_len = 1
    longest_start = weeks[0]
    longest_end = weeks[0]
    current_len = 1
    current_start = weeks[0]
    prev_week = weeks[0]

    for week in weeks[1:]:
        if week == prev_week + timedelta(days=7):
            current_len += 1
        else:
            if current_len > longest_len:
                longest_len = current_len
                longest_start = current_start
                longest_end = prev_week
            current_len = 1
            current_start = week
        prev_week = week

    if current_len > longest_len:
        longest_len = current_len
        longest_start = current_start
        longest_end = prev_week

    return longest_len, longest_start.isoformat(), longest_end.isoformat()


def _longest_rest_gap(conn: sqlite3.Connection) -> tuple[int, str, str] | None:
    """Return the longest workout-free gap between consecutive sessions."""
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM workout_all
        ORDER BY date ASC
        """
    ).fetchall()
    if len(rows) < 2:
        return None

    dates = [date.fromisoformat(row["date"]) for row in rows]
    best_gap_days = -1
    best_start = dates[0]
    best_end = dates[1]
    for previous, current in zip(dates, dates[1:]):
        gap_days = (current - previous).days - 1
        if gap_days > best_gap_days:
            best_gap_days = gap_days
            best_start = previous
            best_end = current

    if best_gap_days < 0:
        return None
    return best_gap_days, best_start.isoformat(), best_end.isoformat()


def compute_milestones(conn: sqlite3.Connection) -> str:
    """Compute lifetime milestones and PR summaries from the database."""
    lines = ["## Milestones (lifetime and PRs)", ""]

    run_pr_lines: list[str] = []
    for km_count, label in [
        (1, "1 km"),
        (5, "5 km"),
        (10, "10 km"),
        (21, "Half marathon"),
    ]:
        row = _best_run_window(conn, km_count)
        if row is None or row["pace_min_km"] is None:
            continue
        run_pr_lines.append(
            f"- {label} PR: **{_format_pace(row['pace_min_km'])}** on {row['date']} ({_format_age_days(row['date'])})."
        )

    if run_pr_lines:
        lines.append("### Run PRs")
        lines.extend(run_pr_lines)
        lines.append("")

    lift_row = conn.execute(
        """
        SELECT date, duration_min
        FROM workout_all
        WHERE category = 'lift' AND duration_min IS NOT NULL
        ORDER BY duration_min DESC, date ASC
        LIMIT 1
        """
    ).fetchone()
    lift_streak = _longest_weekly_streak(conn, "category = 'lift'")

    consistency_lines: list[str] = []
    if lift_row is not None:
        consistency_lines.append(
            f"- Longest lift session: **{lift_row['duration_min']:.0f} min** on {lift_row['date']}."
        )
    if lift_streak is not None:
        streak_len, streak_start, streak_end = lift_streak
        consistency_lines.append(
            f"- Longest lift streak: **{streak_len} weeks** with at least one lift ({streak_start} to {streak_end})."
        )

    lifetime_km_row = conn.execute(
        """
        SELECT SUM(gpx_distance_km) AS value
        FROM workout_all
        WHERE category = 'run' AND gpx_distance_km IS NOT NULL
        """
    ).fetchone()
    if lifetime_km_row and lifetime_km_row["value"] is not None:
        consistency_lines.append(
            f"- Lifetime run volume: **{lifetime_km_row['value']:.1f} km**."
        )

    training_streak = _longest_weekly_streak(conn, "1 = 1")
    if training_streak is not None:
        streak_len, streak_start, streak_end = training_streak
        consistency_lines.append(
            f"- Longest uninterrupted training streak: **{streak_len} weeks** with at least one workout ({streak_start} to {streak_end})."
        )

    rest_gap = _longest_rest_gap(conn)
    if rest_gap is not None:
        gap_days, gap_start, gap_end = rest_gap
        consistency_lines.append(
            f"- Longest rest gap: **{gap_days} days** between {gap_start} and {gap_end}."
        )

    if consistency_lines:
        lines.append("### Consistency")
        lines.extend(consistency_lines)
        lines.append("")

    if lines == ["## Milestones (lifetime and PRs)", ""]:
        return "## Milestones (lifetime and PRs)\n\n- Not enough data yet."

    return "\n".join(lines).rstrip()
