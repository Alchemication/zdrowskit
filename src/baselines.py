"""Auto-computed rolling baseline metrics from the database.

Public API:
    compute_baselines — compute 30/90-day averages and weekly training volume.

Example:
    from baselines import compute_baselines
    md = compute_baselines(conn)
"""

from __future__ import annotations

import sqlite3


def compute_baselines(conn: sqlite3.Connection) -> str:
    """Compute rolling baseline metrics from the database.

    Calculates 30-day and 90-day averages for key health metrics,
    plus weekly training volume aggregates.

    Args:
        conn: Open SQLite database connection.

    Returns:
        A formatted markdown string with baseline tables.
    """
    lines = ["## Baselines (auto-computed from your data)\n"]

    # Daily metrics — 30-day and 90-day averages
    daily_metrics = [
        ("Resting HR", "resting_hr", "bpm", 0),
        ("HRV (SDNN)", "hrv_ms", "ms", 1),
        ("Recovery Index", "recovery_index", "", 2),
        ("VO2max", "vo2max", "ml/kg/min", 1),
        ("Walking HR", "walking_hr_avg", "bpm", 0),
        ("Steps", "steps", "", 0),
        ("Walking Speed", "walking_speed_kmh", "km/h", 1),
    ]

    lines.append("| Metric | 30-day avg | 90-day avg | Unit |")
    lines.append("|--------|-----------|-----------|------|")

    for label, col, unit, decimals in daily_metrics:
        vals = {}
        for period, days in [("30d", 30), ("90d", 90)]:
            row = conn.execute(
                f"SELECT AVG({col}) FROM daily "  # noqa: S608
                f"WHERE {col} IS NOT NULL "
                f"AND date >= date('now', '-{days} days')",
            ).fetchone()
            vals[period] = row[0] if row and row[0] is not None else None

        fmt_30 = f"{vals['30d']:.{decimals}f}" if vals["30d"] is not None else "—"
        fmt_90 = f"{vals['90d']:.{decimals}f}" if vals["90d"] is not None else "—"
        lines.append(f"| {label} | {fmt_30} | {fmt_90} | {unit} |")

    # Weekly training volume — averages over last 4 and 12 weeks
    lines.append("")
    lines.append("| Training Volume | Last 4 weeks avg | Last 12 weeks avg |")
    lines.append("|-----------------|-------------------|-------------------|")

    volume_queries = [
        (
            "Run distance",
            "km/week",
            "SELECT SUM(gpx_distance_km) FROM workout "
            "WHERE category = 'run' AND gpx_distance_km IS NOT NULL "
            "AND date >= date('now', '-{days} days')",
        ),
        (
            "Run sessions",
            "/week",
            "SELECT COUNT(*) FROM workout "
            "WHERE category = 'run' "
            "AND date >= date('now', '-{days} days')",
        ),
        (
            "Lift sessions",
            "/week",
            "SELECT COUNT(*) FROM workout "
            "WHERE category = 'lift' "
            "AND date >= date('now', '-{days} days')",
        ),
        (
            "Lift duration",
            "min/week",
            "SELECT SUM(duration_min) FROM workout "
            "WHERE category = 'lift' AND duration_min IS NOT NULL "
            "AND date >= date('now', '-{days} days')",
        ),
    ]

    for label, unit, query_template in volume_queries:
        vals = {}
        for period, days, weeks in [("4w", 28, 4), ("12w", 84, 12)]:
            row = conn.execute(query_template.format(days=days)).fetchone()
            total = row[0] if row and row[0] is not None else 0
            vals[period] = total / weeks
        lines.append(
            f"| {label} | {vals['4w']:.1f} {unit} | {vals['12w']:.1f} {unit} |"
        )

    # Best recent pace (from runs with GPX data, last 30 days)
    pace_row = conn.execute(
        "SELECT MIN(duration_min / gpx_distance_km) FROM workout "
        "WHERE category = 'run' AND gpx_distance_km > 0 "
        "AND date >= date('now', '-30 days')"
    ).fetchone()
    if pace_row and pace_row[0] is not None:
        pace = pace_row[0]
        pace_min = int(pace)
        pace_sec = int((pace - pace_min) * 60)
        lines.append(f"\n**Best pace (30d):** {pace_min}:{pace_sec:02d} min/km")

    return "\n".join(lines)
