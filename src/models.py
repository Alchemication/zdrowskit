"""Data models for the Apple Health pipeline.

Public API:
    WorkoutSplit    -- per-km pacing and elevation stats for route-based workouts
    WorkoutSnapshot -- per-workout metrics including optional GPX-derived stats
    DailySnapshot   -- all metrics for a single calendar day
    WeeklySummary   -- aggregated week-level stats

Example:
    from models import DailySnapshot, WeeklySummary

    day = DailySnapshot(date="2026-03-13", steps=9500, resting_hr=52)
    summary = WeeklySummary(week_label="2026-W11 (2026-03-09 – 2026-03-15)")
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class WorkoutSplit:
    """Per-km pacing and elevation stats for a route-based workout.

    Attributes:
        km_index: 1-based split number (1 = first kilometre).
        pace_min_km: Minutes required to cover this kilometre.
        avg_speed_ms: Average speed in m/s across the split.
        elevation_gain_m: Positive elevation gain in metres inside the split.
        elevation_loss_m: Negative elevation loss in metres inside the split.
    """

    km_index: int
    pace_min_km: float
    avg_speed_ms: float | None = None
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None


@dataclass
class WorkoutSnapshot:
    """Per-workout metrics, optionally enriched with GPX-derived stats.

    Attributes:
        type: Human-readable workout name, e.g. "Outdoor Run".
        category: Normalised bucket: "run" | "lift" | "walk" | "cycle" | "other".
        counts_as_lift: Whether zdrowskit should treat this as a completed
            strength session for weekly planning and summaries. If omitted,
            it is derived from workout type and duration.
        start_utc: ISO 8601 UTC start time, e.g. "2026-03-10T17:04:05Z".
        duration_min: Elapsed workout time in minutes.
        hr_min: Minimum heart rate (bpm) during the workout.
        hr_avg: Average heart rate (bpm) during the workout.
        hr_max: Maximum heart rate (bpm) during the workout.
        active_energy_kj: Active energy burned in kilojoules.
        intensity_kcal_per_hr_kg: Apple intensity metric (kcal/hr/kg).
        temperature_c: Ambient temperature in Celsius at workout time.
        humidity_pct: Relative humidity percentage at workout time.
        gpx_distance_km: Total route distance in km (None if no GPX file matched).
        gpx_elevation_gain_m: Positive elevation gain in metres from GPX.
        gpx_avg_speed_ms: Mean speed in m/s from GPX speed field.
        gpx_max_speed_p95_ms: 95th-percentile speed in m/s (filters GPS spikes).
        splits: Derived per-km splits for route-bearing workouts.
    """

    type: str  # "Outdoor Run", "Traditional Strength Training", etc.
    category: str  # "run" | "lift" | "walk" | "other"
    start_utc: str  # ISO datetime string
    duration_min: float
    counts_as_lift: bool | None = None
    hr_min: int | None = None
    hr_avg: float | None = None
    hr_max: int | None = None
    active_energy_kj: float = 0.0
    intensity_kcal_per_hr_kg: float | None = None
    temperature_c: float | None = None
    humidity_pct: int | None = None
    # GPX-derived — None if no matching route file
    gpx_distance_km: float | None = None
    gpx_elevation_gain_m: float | None = None
    gpx_avg_speed_ms: float | None = None
    gpx_max_speed_p95_ms: float | None = None
    splits: list[WorkoutSplit] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Derive lift-counting semantics when not explicitly provided."""
        if self.counts_as_lift is not None:
            return

        normalized = self.type.lower()
        if normalized == "traditional strength training":
            self.counts_as_lift = True
        elif normalized == "functional strength training":
            self.counts_as_lift = self.duration_min >= 15.0
        else:
            self.counts_as_lift = False


@dataclass
class DailySnapshot:
    """All health metrics for a single calendar day.

    Attributes:
        date: ISO date string, e.g. "2026-03-13".
        steps: Total step count.
        distance_km: Total walking/running distance in km.
        active_energy_kj: Active energy burned in kilojoules.
        exercise_min: Apple exercise ring minutes.
        stand_hours: Apple stand ring hours.
        flights_climbed: Flights of stairs climbed.
        resting_hr: Resting heart rate in bpm.
        hrv_ms: Heart rate variability (SDNN) in milliseconds.
        walking_hr_avg: Average heart rate while walking (bpm).
        hr_day_min: Minimum heart rate recorded that day (bpm).
        hr_day_max: Maximum heart rate recorded that day (bpm).
        vo2max: VO2 max estimate (ml/kg/min); sparse — only recorded on run days.
        walking_speed_kmh: Average walking speed in km/h.
        walking_step_length_cm: Average walking step length in cm.
        walking_asymmetry_pct: Walking asymmetry percentage.
        walking_double_support_pct: Double-support percentage during walking.
        stair_speed_up_ms: Stair ascent speed in m/s.
        stair_speed_down_ms: Stair descent speed in m/s.
        running_stride_length_m: Running stride length in metres; sparse.
        running_power_w: Running power in watts; sparse.
        running_speed_kmh: Running speed in km/h; sparse.
        sleep_total_h: Total sleep duration in hours (excludes awake segments).
        sleep_in_bed_h: Total time in bed in hours (includes awake segments).
        sleep_efficiency_pct: Sleep efficiency (sleep_total_h / sleep_in_bed_h * 100).
        sleep_deep_h: Hours in Deep sleep stage.
        sleep_core_h: Hours in Core (light) sleep stage.
        sleep_rem_h: Hours in REM sleep stage.
        sleep_awake_h: Hours in Awake stage during the sleep session.
        workouts: List of WorkoutSnapshots that occurred on this day.
        recovery_index: Derived metric: hrv_ms / resting_hr.
    """

    date: str  # "2026-03-13"
    # Activity rings
    steps: int | None = None
    distance_km: float | None = None
    active_energy_kj: float | None = None
    exercise_min: int | None = None
    stand_hours: int | None = None
    flights_climbed: float | None = None
    # Cardiac
    resting_hr: int | None = None
    hrv_ms: float | None = None
    walking_hr_avg: float | None = None
    hr_day_min: int | None = None
    hr_day_max: int | None = None
    vo2max: float | None = None  # sparse — run days only
    # Mobility (daily averages from Apple)
    walking_speed_kmh: float | None = None
    walking_step_length_cm: float | None = None
    walking_asymmetry_pct: float | None = None
    walking_double_support_pct: float | None = None
    stair_speed_up_ms: float | None = None
    stair_speed_down_ms: float | None = None
    running_stride_length_m: float | None = None  # sparse
    running_power_w: float | None = None  # sparse
    running_speed_kmh: float | None = None  # sparse
    # Sleep (from Apple Watch sleep tracking; None for days without data)
    sleep_total_h: float | None = None
    sleep_in_bed_h: float | None = None
    sleep_efficiency_pct: float | None = None
    sleep_deep_h: float | None = None
    sleep_core_h: float | None = None
    sleep_rem_h: float | None = None
    sleep_awake_h: float | None = None
    # Workouts on this day
    workouts: list[WorkoutSnapshot] = field(default_factory=list)
    # Derived
    recovery_index: float | None = None  # hrv_ms / resting_hr


@dataclass
class WeeklySummary:
    """Aggregated health and fitness stats for a single week.

    Attributes:
        week_label: Human-readable label, e.g. "2026-W11 (2026-03-09 – 2026-03-15)".
        run_count: Number of run workouts in the week.
        lift_count: Number of strength/lift workouts in the week.
        walk_count: Number of walk workouts in the week.
        total_run_km: Sum of GPX distances across all runs (km).
        avg_run_km: Mean GPX distance per run (km).
        best_pace_min_per_km: Fastest pace across all runs (min/km); None if no GPX data.
        avg_run_hr: Mean average heart rate across all runs (bpm).
        peak_run_hr: Highest hr_max recorded across all runs (bpm).
        avg_elevation_gain_m: Mean GPX elevation gain per run (m).
        avg_running_power_w: Mean running power from mobility metrics (W).
        avg_running_stride_m: Mean running stride length from mobility metrics (m).
        avg_run_temp_c: Mean ambient temperature across runs (°C).
        avg_run_humidity_pct: Mean humidity across runs (%).
        total_lift_min: Total lifting time across the week (min).
        avg_lift_hr: Mean average heart rate across all lifts (bpm).
        avg_steps: Mean daily step count.
        avg_active_energy_kj: Mean daily active energy (kJ).
        avg_exercise_min: Mean daily Apple exercise ring minutes.
        avg_stand_hours: Mean daily Apple stand ring hours.
        avg_resting_hr: Mean daily resting HR (bpm); None-safe over available days.
        avg_hrv_ms: Mean daily HRV (ms); None-safe over available days.
        avg_walking_hr: Mean daily walking HR (bpm).
        latest_vo2max: Most recent VO2 max value recorded in the week.
        avg_recovery_index: Mean of hrv_ms / resting_hr across days.
        hrv_trend: Linear-regression direction: "improving" | "declining" | "stable" | None.
        avg_sleep_total_h: Mean nightly sleep duration (hours); None if no sleep data.
        avg_sleep_efficiency_pct: Mean sleep efficiency percentage.
        avg_sleep_deep_h: Mean nightly Deep sleep (hours).
        avg_sleep_core_h: Mean nightly Core/light sleep (hours).
        avg_sleep_rem_h: Mean nightly REM sleep (hours).
        avg_sleep_awake_h: Mean nightly awake time during sleep (hours).
    """

    week_label: str  # "2026-W11 (2026-03-09 – 2026-03-15)"
    # Workout counts
    run_count: int = 0
    lift_count: int = 0
    walk_count: int = 0
    # Run aggregates
    total_run_km: float = 0.0
    avg_run_km: float = 0.0
    best_pace_min_per_km: float | None = None
    avg_run_hr: float | None = None
    peak_run_hr: int | None = None
    avg_elevation_gain_m: float | None = None
    avg_running_power_w: float | None = None
    avg_running_stride_m: float | None = None
    avg_run_temp_c: float | None = None
    avg_run_humidity_pct: float | None = None
    # Lift aggregates
    total_lift_min: float = 0.0
    avg_lift_hr: float | None = None
    # Activity ring averages
    avg_steps: int = 0
    avg_active_energy_kj: float = 0.0
    avg_exercise_min: float = 0.0
    avg_stand_hours: float = 0.0
    # Cardiac averages (None-safe: days with missing data are skipped)
    avg_resting_hr: float | None = None
    avg_hrv_ms: float | None = None
    avg_walking_hr: float | None = None
    latest_vo2max: float | None = None
    # Derived
    avg_recovery_index: float | None = None
    hrv_trend: str | None = None  # "improving" | "declining" | "stable"
    # Sleep averages (None-safe: days without sleep data are skipped)
    avg_sleep_total_h: float | None = None
    avg_sleep_efficiency_pct: float | None = None
    avg_sleep_deep_h: float | None = None
    avg_sleep_core_h: float | None = None
    avg_sleep_rem_h: float | None = None
    avg_sleep_awake_h: float | None = None
