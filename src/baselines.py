"""Auto-computed rolling and seasonal baseline metrics from the database.

Public API:
    compute_baselines — compute rolling, year-over-year, and split-derived baselines

Example:
    from baselines import compute_baselines
    md = compute_baselines(conn)
"""

from __future__ import annotations

import sqlite3


_DAILY_METRICS = [
    ("Resting HR", "resting_hr", "bpm", 0),
    ("HRV (SDNN)", "hrv_ms", "ms", 1),
    ("Recovery Index", "recovery_index", "", 2),
    ("VO2max", "vo2max", "ml/kg/min", 1),
    ("Walking HR", "walking_hr_avg", "bpm", 0),
    ("Steps", "steps", "", 0),
    ("Walking Speed", "walking_speed_kmh", "km/h", 1),
    ("Sleep Duration", "sleep_total_h", "hr", 2),
    ("Sleep Efficiency", "sleep_efficiency_pct", "%", 1),
    ("Deep Sleep", "sleep_deep_h", "hr", 2),
    ("REM Sleep", "sleep_rem_h", "hr", 2),
]

# Columns where a literal 0 means "not tracked" rather than a real observation.
# Apple Health writes zero-valued sleep rows for untracked nights, which would
# otherwise drag baselines toward zero.
_ZERO_IS_NO_DATA = {
    "sleep_total_h",
    "sleep_in_bed_h",
    "sleep_efficiency_pct",
    "sleep_deep_h",
    "sleep_core_h",
    "sleep_rem_h",
    "sleep_awake_h",
}

_TRAINING_VOLUME_QUERIES = [
    (
        "Run distance",
        "km/week",
        "SELECT SUM(gpx_distance_km) AS value "
        "FROM workout "
        "WHERE category = 'run' AND gpx_distance_km IS NOT NULL "
        "AND date >= date('now', ?)",
    ),
    (
        "Run sessions",
        "/week",
        "SELECT COUNT(*) AS value "
        "FROM workout "
        "WHERE category = 'run' "
        "AND date >= date('now', ?)",
    ),
    (
        "Lift sessions",
        "/week",
        "SELECT COUNT(*) AS value "
        "FROM workout "
        "WHERE category = 'lift' "
        "AND date >= date('now', ?)",
    ),
    (
        "Lift duration",
        "min/week",
        "SELECT SUM(duration_min) AS value "
        "FROM workout "
        "WHERE category = 'lift' AND duration_min IS NOT NULL "
        "AND date >= date('now', ?)",
    ),
]


def _fmt(value: float | None, decimals: int) -> str:
    """Format a baseline number or em dash when unavailable."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f}"


def _query_daily_avg(
    conn: sqlite3.Connection,
    column: str,
    start_modifiers: tuple[str, ...],
    end_modifiers: tuple[str, ...] = ("0 days",),
    min_samples: int = 1,
) -> float | None:
    """Return a daily average over a relative SQLite date window.

    When fewer than ``min_samples`` non-null values exist in the window, return
    None so a single sparse observation cannot dominate the average. Callers
    comparing against historical windows (e.g. YoY) should set this to at least
    7 to avoid rendering a noisy single-day reading as a 30-day baseline.
    """
    start_expr = "date('now'" + "".join(", ?" for _ in start_modifiers) + ")"
    end_expr = "date('now'" + "".join(", ?" for _ in end_modifiers) + ")"
    zero_filter = f" AND {column} != 0" if column in _ZERO_IS_NO_DATA else ""
    row = conn.execute(
        f"""
        SELECT AVG({column}) AS value, COUNT({column}) AS n
        FROM daily
        WHERE {column} IS NOT NULL{zero_filter}
          AND date BETWEEN {start_expr} AND {end_expr}
        """,  # noqa: S608
        (*start_modifiers, *end_modifiers),
    ).fetchone()
    if not row or row["value"] is None or row["n"] < min_samples:
        return None
    return row["value"]


def _query_window_value(
    conn: sqlite3.Connection,
    query: str,
    start_modifiers: tuple[str, ...],
    end_modifiers: tuple[str, ...],
) -> float | None:
    """Return a scalar value over a relative SQLite date window."""
    start_expr = "date('now'" + "".join(", ?" for _ in start_modifiers) + ")"
    end_expr = "date('now'" + "".join(", ?" for _ in end_modifiers) + ")"
    row = conn.execute(
        query.format(start_expr=start_expr, end_expr=end_expr),
        (*start_modifiers, *end_modifiers),
    ).fetchone()
    return row["value"] if row and row["value"] is not None else None


def _append_daily_metrics(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append the 30d/90d daily-metrics table."""
    lines.append("| Metric | 30-day avg | 90-day avg | Unit |")
    lines.append("|--------|-----------|-----------|------|")

    for label, column, unit, decimals in _DAILY_METRICS:
        avg_30 = _query_daily_avg(conn, column, ("-30 days",))
        avg_90 = _query_daily_avg(conn, column, ("-90 days",))
        lines.append(
            f"| {label} | {_fmt(avg_30, decimals)} | {_fmt(avg_90, decimals)} | {unit} |"
        )


def _append_sleep_compliance(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append sleep tracking compliance for recent periods."""
    compliance_values: dict[str, str] = {}
    for period, days in [("30d", 30), ("90d", 90)]:
        row = conn.execute(
            "SELECT "
            "  COUNT(CASE WHEN sleep_total_h IS NOT NULL THEN 1 END) AS tracked,"
            "  COUNT(*) AS total "
            "FROM daily "
            "WHERE date >= date('now', ?) "
            "AND date < date('now')",
            (f"-{days} days",),
        ).fetchone()
        tracked, total = (row["tracked"], row["total"]) if row else (0, 0)
        pct = (tracked / total * 100) if total > 0 else 0
        compliance_values[period] = f"{tracked}/{total} ({pct:.0f}%)"

    lines.append(
        f"\n**Sleep tracking compliance:** "
        f"{compliance_values['30d']} last 30d, "
        f"{compliance_values['90d']} last 90d"
    )


def _append_training_volume(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append recent training-volume averages."""
    lines.append("")
    lines.append("| Training Volume | Last 4 weeks avg | Last 12 weeks avg |")
    lines.append("|-----------------|-------------------|-------------------|")

    for label, unit, query in _TRAINING_VOLUME_QUERIES:
        values: dict[str, float] = {}
        for period, days, weeks in [("4w", 28, 4), ("12w", 84, 12)]:
            row = conn.execute(query, (f"-{days} days",)).fetchone()
            total = row["value"] if row and row["value"] is not None else 0.0
            values[period] = total / weeks
        lines.append(
            f"| {label} | {values['4w']:.1f} {unit} | {values['12w']:.1f} {unit} |"
        )


def _append_yoy_daily_metrics(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append a same-season year-over-year table for daily metrics."""
    rows: list[str] = []
    for label, column, unit, decimals in _DAILY_METRICS:
        current_30d = _query_daily_avg(conn, column, ("-30 days",))
        year_1 = _query_daily_avg(
            conn,
            column,
            ("-1 year", "-15 days"),
            ("-1 year", "+15 days"),
            min_samples=7,
        )
        year_2 = _query_daily_avg(
            conn,
            column,
            ("-2 years", "-15 days"),
            ("-2 years", "+15 days"),
            min_samples=7,
        )
        if year_1 is None and year_2 is None:
            continue
        rows.append(
            f"| {label} | {_fmt(current_30d, decimals)} | {_fmt(year_1, decimals)} | {_fmt(year_2, decimals)} | {unit} |"
        )

    if not rows:
        return

    lines.append("")
    lines.append("### Same-season comparison")
    lines.append("")
    lines.append(
        "| Metric | Current 30d | Same month last year | Same month 2y ago | Unit |"
    )
    lines.append(
        "|--------|-------------|----------------------|-------------------|------|"
    )
    lines.extend(rows)


def _append_seasonal_training_volume(
    lines: list[str], conn: sqlite3.Connection
) -> None:
    """Append current-vs-prior-years seasonal run-volume comparisons."""
    queries = [
        (
            "Run distance",
            "km",
            "SELECT SUM(gpx_distance_km) AS value "
            "FROM workout "
            "WHERE category = 'run' AND gpx_distance_km IS NOT NULL "
            "AND date BETWEEN {start_expr} AND {end_expr}",
        ),
        (
            "Run sessions",
            "sessions",
            "SELECT COUNT(*) AS value "
            "FROM workout "
            "WHERE category = 'run' "
            "AND date BETWEEN {start_expr} AND {end_expr}",
        ),
    ]

    lines.append("")
    lines.append("### Seasonal run volume")
    lines.append("")
    lines.append(
        "| Training Volume | Current 4w | Same 4w 1y ago | Same 4w 2y ago | Same 4w 3y ago |"
    )
    lines.append(
        "|-----------------|------------|----------------|----------------|----------------|"
    )

    for label, unit, query in queries:
        values = [
            _query_window_value(conn, query, ("-28 days",), ("0 days",)),
            _query_window_value(conn, query, ("-1 year", "-28 days"), ("-1 year",)),
            _query_window_value(conn, query, ("-2 years", "-28 days"), ("-2 years",)),
            _query_window_value(conn, query, ("-3 years", "-28 days"), ("-3 years",)),
        ]
        lines.append(
            "| "
            f"{label} | "
            f"{_fmt(values[0], 1)} {unit} | "
            f"{_fmt(values[1], 1)} {unit} | "
            f"{_fmt(values[2], 1)} {unit} | "
            f"{_fmt(values[3], 1)} {unit} |"
        )


def _append_pace_curve(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append a per-year best 5 km pace curve based on split windows."""
    rows = conn.execute(
        """
        WITH split_windows AS (
            SELECT
                w.date AS workout_date,
                CAST(strftime('%Y', w.date) AS INTEGER) AS calendar_year,
                COUNT(*) OVER (
                    PARTITION BY ws.start_utc
                    ORDER BY ws.km_index
                    ROWS BETWEEN CURRENT ROW AND 4 FOLLOWING
                ) AS split_count,
                SUM(ws.pace_min_km) OVER (
                    PARTITION BY ws.start_utc
                    ORDER BY ws.km_index
                    ROWS BETWEEN CURRENT ROW AND 4 FOLLOWING
                ) AS total_pace_min
            FROM workout_split AS ws
            JOIN workout AS w
              ON w.start_utc = ws.start_utc
            WHERE w.category = 'run'
        ),
        ranked AS (
            SELECT
                calendar_year,
                workout_date,
                total_pace_min / 5.0 AS pace_min_km,
                ROW_NUMBER() OVER (
                    PARTITION BY calendar_year
                    ORDER BY total_pace_min ASC, workout_date ASC
                ) AS row_num
            FROM split_windows
            WHERE split_count = 5
        )
        SELECT calendar_year, workout_date, pace_min_km
        FROM ranked
        WHERE row_num = 1
        ORDER BY calendar_year ASC
        """
    ).fetchall()

    if not rows:
        return

    lines.append("")
    lines.append("### Annual best 5 km pace")
    lines.append("")
    lines.append("| Year | Best 5 km pace | Date |")
    lines.append("|------|----------------|------|")
    for row in rows:
        pace = row["pace_min_km"]
        pace_min = int(pace)
        pace_sec = int(round((pace - pace_min) * 60))
        if pace_sec == 60:
            pace_min += 1
            pace_sec = 0
        lines.append(
            f"| {row['calendar_year']} | {pace_min}:{pace_sec:02d}/km | {row['workout_date']} |"
        )


def _append_best_recent_pace(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append the recent best pace summary line."""
    row = conn.execute(
        "SELECT MIN(duration_min / gpx_distance_km) AS pace_min_km "
        "FROM workout "
        "WHERE category = 'run' AND gpx_distance_km > 0 "
        "AND date >= date('now', '-30 days')"
    ).fetchone()
    if not row or row["pace_min_km"] is None:
        return

    pace = row["pace_min_km"]
    pace_min = int(pace)
    pace_sec = int(round((pace - pace_min) * 60))
    if pace_sec == 60:
        pace_min += 1
        pace_sec = 0
    lines.append(f"\n**Best pace (30d):** {pace_min}:{pace_sec:02d} min/km")


def compute_baselines(conn: sqlite3.Connection) -> str:
    """Compute rolling baseline metrics from the database.

    Args:
        conn: Open SQLite database connection.

    Returns:
        A formatted markdown string with rolling, seasonal, and split-derived
        baseline tables.
    """
    lines = ["## Baselines (auto-computed from your data)\n"]
    _append_daily_metrics(lines, conn)
    _append_sleep_compliance(lines, conn)
    _append_training_volume(lines, conn)
    _append_yoy_daily_metrics(lines, conn)
    _append_seasonal_training_volume(lines, conn)
    _append_pace_curve(lines, conn)
    _append_best_recent_pace(lines, conn)
    return "\n".join(lines)
