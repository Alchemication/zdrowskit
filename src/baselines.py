"""Auto-computed rolling and seasonal baseline metrics from the database.

Public API:
    compute_baselines — compute rolling, year-over-year, and split-derived baselines
    days_of_data — inclusive span of stored history
    metric_sample_counts — readings per daily metric in a trailing window

Example:
    from baselines import compute_baselines
    md = compute_baselines(conn)
"""

from __future__ import annotations

import sqlite3

from config import BASELINE_MIN_SAMPLES, BASELINE_MIN_WINDOW_COVERAGE


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


def _metric_counts(conn: sqlite3.Connection, days: int) -> list[tuple[str, str, int]]:
    """Return (label, column, readings) for every daily metric in a window."""
    counts: list[tuple[str, str, int]] = []
    for label, column, _unit, _decimals in _DAILY_METRICS:
        zero_filter = f" AND {column} != 0" if column in _ZERO_IS_NO_DATA else ""
        row = conn.execute(
            f"""
            SELECT COUNT({column}) AS n
            FROM daily
            WHERE {column} IS NOT NULL{zero_filter}
              AND date >= date('now', ?)
            """,  # noqa: S608
            (f"-{days} days",),
        ).fetchone()
        counts.append((label, column, int(row["n"]) if row and row["n"] else 0))
    return counts


def metric_sample_counts(conn: sqlite3.Connection, days: int) -> dict[str, int]:
    """Return how many readings each daily metric has in a trailing window.

    Exposed so callers can describe what is and is not yet knowable about a
    profile without duplicating the metric list or the zero-means-untracked
    rule that decides what counts as a reading.

    Args:
        conn: Open SQLite database connection.
        days: Length of the trailing window in days.

    Returns:
        A mapping of human-readable metric label to reading count.
    """
    return {label: n for label, _column, n in _metric_counts(conn, days)}


def unestablished_metrics(conn: sqlite3.Connection, days: int) -> set[str]:
    """Return daily metric columns with too few readings to define a normal.

    Callers rendering per-day values use this to avoid printing a short run of
    readings as a consecutive series. Three numbers in date order are read as a
    slope no matter what surrounding text says about sample size.

    Args:
        conn: Open SQLite database connection.
        days: Length of the trailing window in days.

    Returns:
        Column names holding fewer than ``BASELINE_MIN_SAMPLES`` readings.
    """
    return {
        column
        for _label, column, n in _metric_counts(conn, days)
        if n < BASELINE_MIN_SAMPLES
    }


def days_of_data(conn: sqlite3.Connection) -> int:
    """Return how many days of history this profile covers, inclusive.

    Args:
        conn: Open SQLite database connection.

    Returns:
        Days from the earliest stored day through today, or 0 when the
        ``daily`` table is empty.
    """
    row = conn.execute(
        "SELECT CAST(julianday('now') - julianday(MIN(date)) AS INTEGER) AS span "
        "FROM daily"
    ).fetchone()
    if not row or row["span"] is None:
        return 0
    return max(int(row["span"]), 0) + 1


def _append_daily_metrics(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append the 30d/90d daily-metrics table.

    A profile too new for any metric to clear the sample floor gets a sentence
    instead of the table. Eleven rows of dashes invite the reader to treat the
    absence as a finding about the person rather than about the history.
    """
    rows: list[str] = []
    has_value = False
    for label, column, unit, decimals in _DAILY_METRICS:
        avg_30 = _query_daily_avg(
            conn, column, ("-30 days",), min_samples=BASELINE_MIN_SAMPLES
        )
        avg_90 = _query_daily_avg(
            conn, column, ("-90 days",), min_samples=BASELINE_MIN_SAMPLES
        )
        has_value = has_value or avg_30 is not None or avg_90 is not None
        rows.append(
            f"| {label} | {_fmt(avg_30, decimals)} | {_fmt(avg_90, decimals)} | {unit} |"
        )

    if not has_value:
        covered = days_of_data(conn)
        lines.append(
            f"No rolling averages yet: {covered} "
            f"{'day' if covered == 1 else 'days'} of history, and a baseline "
            f"needs at least {BASELINE_MIN_SAMPLES} readings of a metric before "
            "it means anything."
        )
        return

    lines.append("| Metric | 30-day avg | 90-day avg | Unit |")
    lines.append("|--------|-----------|-----------|------|")
    lines.extend(rows)


def _append_sleep_compliance(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append sleep tracking compliance for recent periods.

    The denominator is the window itself, not the rows that happen to exist in
    it. Counting only present rows made every profile look perfectly compliant,
    including one holding two nights. A short history is disclosed separately so
    a low percentage reads as missing coverage rather than as sloppy tracking.
    """
    compliance_values: dict[str, str] = {}
    for period, days in [("30d", 30), ("90d", 90)]:
        row = conn.execute(
            "SELECT COUNT(CASE WHEN sleep_total_h IS NOT NULL THEN 1 END) AS tracked "
            "FROM daily "
            "WHERE date >= date('now', ?) "
            "AND date < date('now')",
            (f"-{days} days",),
        ).fetchone()
        tracked = row["tracked"] if row else 0
        pct = tracked / days * 100
        compliance_values[period] = f"{tracked}/{days} ({pct:.0f}%)"

    covered = days_of_data(conn)
    if covered < 90 * BASELINE_MIN_WINDOW_COVERAGE:
        suffix = (
            f" — this profile only covers {covered} "
            f"{'day' if covered == 1 else 'days'}, so both denominators "
            "include days before it had any data"
        )
    else:
        suffix = ""
    lines.append(
        f"\n**Sleep tracking compliance:** "
        f"{compliance_values['30d']} last 30d, "
        f"{compliance_values['90d']} last 90d{suffix}"
    )


def _append_training_volume(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append recent training-volume averages.

    A window is only divided by its nominal length once the data actually
    covers it. Otherwise a few days of history would be presented as a
    twelve-week habit. Profiles with no workouts at all get no table: four rows
    of zeroed run and lift volume describe a runner who stopped, not someone
    who has never trained.
    """
    if not _has_workouts(conn):
        return

    covered = days_of_data(conn)
    if covered < 28 * BASELINE_MIN_WINDOW_COVERAGE:
        return

    lines.append("")
    lines.append("| Training Volume | Last 4 weeks avg | Last 12 weeks avg |")
    lines.append("|-----------------|-------------------|-------------------|")

    for label, unit, query in _TRAINING_VOLUME_QUERIES:
        cells: list[str] = []
        for days, weeks in [(28, 4), (84, 12)]:
            if covered < days * BASELINE_MIN_WINDOW_COVERAGE:
                cells.append("—")
                continue
            row = conn.execute(query, (f"-{days} days",)).fetchone()
            total = row["value"] if row and row["value"] is not None else 0.0
            cells.append(f"{total / weeks:.1f} {unit}")
        lines.append(f"| {label} | {cells[0]} | {cells[1]} |")


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


def _has_workouts(conn: sqlite3.Connection) -> bool:
    """Return whether this profile has recorded any workout at all."""
    return conn.execute("SELECT 1 FROM workout LIMIT 1").fetchone() is not None


def _append_seasonal_training_volume(
    lines: list[str], conn: sqlite3.Connection
) -> None:
    """Append current-vs-prior-years seasonal run-volume comparisons.

    Emitted only when some prior year has data to compare against. A profile
    younger than a year would otherwise get a heading over a grid of dashes.
    """
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

    # COUNT(*) rows always return 0 rather than NULL, so emptiness has to be
    # decided from the data rather than from the rendered cells.
    prior_year = conn.execute(
        "SELECT 1 FROM workout WHERE date < date('now', '-1 year') LIMIT 1"
    ).fetchone()
    if prior_year is None:
        return

    rows: list[str] = []
    for label, unit, query in queries:
        values = [
            _query_window_value(conn, query, ("-28 days",), ("0 days",)),
            _query_window_value(conn, query, ("-1 year", "-28 days"), ("-1 year",)),
            _query_window_value(conn, query, ("-2 years", "-28 days"), ("-2 years",)),
            _query_window_value(conn, query, ("-3 years", "-28 days"), ("-3 years",)),
        ]
        rows.append(
            "| "
            f"{label} | "
            f"{_fmt(values[0], 1)} {unit} | "
            f"{_fmt(values[1], 1)} {unit} | "
            f"{_fmt(values[2], 1)} {unit} | "
            f"{_fmt(values[3], 1)} {unit} |"
        )

    lines.append("")
    lines.append("### Seasonal run volume")
    lines.append("")
    lines.append(
        "| Training Volume | Current 4w | Same 4w 1y ago | Same 4w 2y ago | Same 4w 3y ago |"
    )
    lines.append(
        "|-----------------|------------|----------------|----------------|----------------|"
    )
    lines.extend(rows)


def _append_pace_curve(lines: list[str], conn: sqlite3.Connection) -> None:
    """Append a per-year best 5 km pace curve based on split windows.

    A year needs enough qualifying runs to have a best worth naming. Otherwise
    a single run becomes that year's record, and the table implies a season of
    efforts it was picked from.
    """
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
        qualifying AS (
            SELECT calendar_year, COUNT(DISTINCT workout_date) AS runs
            FROM split_windows
            WHERE split_count = 5
            GROUP BY calendar_year
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
        SELECT ranked.calendar_year, ranked.workout_date, ranked.pace_min_km
        FROM ranked
        JOIN qualifying ON qualifying.calendar_year = ranked.calendar_year
        WHERE ranked.row_num = 1 AND qualifying.runs >= ?
        ORDER BY ranked.calendar_year ASC
        """,
        (BASELINE_MIN_SAMPLES,),
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
    """Append the recent best pace summary line.

    "Best" needs a field to be best of. With one or two runs in the window the
    number is simply the most recent run wearing a superlative.
    """
    row = conn.execute(
        "SELECT MIN(duration_min / gpx_distance_km) AS pace_min_km, "
        "  COUNT(*) AS runs "
        "FROM workout "
        "WHERE category = 'run' AND gpx_distance_km > 0 "
        "AND date >= date('now', '-30 days')"
    ).fetchone()
    if not row or row["pace_min_km"] is None:
        return
    if row["runs"] < BASELINE_MIN_SAMPLES:
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
