"""Tests for src/aggregator.py."""

from __future__ import annotations

from models import DailySnapshot, WorkoutSnapshot
from aggregator import (
    _best_run_pace,
    _hrv_trend,
    _nonnull,
    _safe_mean,
    _week_label,
    summarise,
)


class TestNonnull:
    def test_filters_nones(self) -> None:
        assert _nonnull([1, None, 3, None, 5]) == [1, 3, 5]

    def test_all_none(self) -> None:
        assert _nonnull([None, None]) == []

    def test_empty(self) -> None:
        assert _nonnull([]) == []

    def test_no_nones(self) -> None:
        assert _nonnull([1, 2, 3]) == [1, 2, 3]


class TestWeekLabel:
    def test_full_week(self) -> None:
        snaps = [DailySnapshot(date=f"2026-03-{d:02d}") for d in range(9, 16)]
        label = _week_label(snaps)
        assert "2026-W11" in label
        assert "2026-03-09" in label
        assert "2026-03-15" in label

    def test_single_day(self) -> None:
        label = _week_label([DailySnapshot(date="2026-01-01")])
        assert "2026-W01" in label
        assert "2026-01-01" in label

    def test_empty(self) -> None:
        assert _week_label([]) == "unknown"

    def test_unordered_snapshots(self) -> None:
        snaps = [
            DailySnapshot(date="2026-03-15"),
            DailySnapshot(date="2026-03-09"),
            DailySnapshot(date="2026-03-12"),
        ]
        label = _week_label(snaps)
        # Should sort and use min/max
        assert "2026-03-09" in label
        assert "2026-03-15" in label


class TestSafeMean:
    def test_with_nones(self) -> None:
        assert _safe_mean([10.0, None, 20.0]) == 15.0

    def test_all_none(self) -> None:
        assert _safe_mean([None, None]) is None

    def test_empty(self) -> None:
        assert _safe_mean([]) is None

    def test_single_value(self) -> None:
        assert _safe_mean([42.0]) == 42.0


class TestHrvTrend:
    def _make_snapshots(self, hrv_values: list[float | None]) -> list[DailySnapshot]:
        return [
            DailySnapshot(date=f"2026-03-{9 + i:02d}", hrv_ms=v)
            for i, v in enumerate(hrv_values)
        ]

    def test_improving(self) -> None:
        # Clear upward trend: 40, 45, 50, 55, 60, 65, 70
        result = _hrv_trend(self._make_snapshots([40, 45, 50, 55, 60, 65, 70]))
        assert result == "improving"

    def test_declining(self) -> None:
        # Clear downward trend
        result = _hrv_trend(self._make_snapshots([70, 65, 60, 55, 50, 45, 40]))
        assert result == "declining"

    def test_stable(self) -> None:
        # Flat values
        result = _hrv_trend(self._make_snapshots([55.0, 55.1, 55.0, 55.2, 55.0]))
        assert result == "stable"

    def test_insufficient_data(self) -> None:
        result = _hrv_trend(self._make_snapshots([55.0, 60.0]))
        assert result is None

    def test_skips_none_values(self) -> None:
        # 5 values but only 2 non-None → insufficient
        result = _hrv_trend(self._make_snapshots([55.0, None, None, None, 60.0]))
        assert result is None


class TestBestRunPace:
    def test_with_gpx_data(self) -> None:
        runs = [
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-03-10T07:00:00Z",
                duration_min=35.0,
                gpx_distance_km=5.0,
            ),
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-03-12T07:00:00Z",
                duration_min=30.0,
                gpx_distance_km=5.0,
            ),
        ]
        # 30/5 = 6.0 min/km is faster than 35/5 = 7.0
        assert _best_run_pace(runs) == 6.0

    def test_no_gpx_data(self) -> None:
        runs = [
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-03-10T07:00:00Z",
                duration_min=35.0,
            ),
        ]
        assert _best_run_pace(runs) is None


class TestBestRunPaceZeroDistance:
    def test_zero_distance_excluded(self) -> None:
        """A run with gpx_distance_km == 0.0 (falsy) must not cause division by zero."""
        runs = [
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-03-10T07:00:00Z",
                duration_min=35.0,
                gpx_distance_km=0.0,
            ),
        ]
        assert _best_run_pace(runs) is None

    def test_zero_distance_mixed_with_valid(self) -> None:
        runs = [
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-03-10T07:00:00Z",
                duration_min=35.0,
                gpx_distance_km=0.0,
            ),
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-03-12T07:00:00Z",
                duration_min=30.0,
                gpx_distance_km=5.0,
            ),
        ]
        assert _best_run_pace(runs) == 6.0


class TestSummarise:
    def test_full_week(self, sample_snapshots: list[DailySnapshot]) -> None:
        summary = summarise(sample_snapshots)
        assert summary.run_count == 1
        assert summary.lift_count == 1
        assert summary.avg_resting_hr is not None
        assert summary.avg_hrv_ms is not None
        assert summary.hrv_trend is not None
        assert summary.run_consistency_pct == 50.0  # 1/2 * 100
        assert summary.lift_consistency_pct == 50.0

    def test_empty_snapshots(self) -> None:
        summary = summarise([])
        assert summary.run_count == 0
        assert summary.lift_count == 0
        assert summary.avg_resting_hr is None
        assert summary.avg_hrv_ms is None
        assert summary.week_label == "unknown"

    def test_consistency_caps_at_100(self) -> None:
        """Exceeding weekly targets should cap at 100%, not go higher."""
        snaps = [
            DailySnapshot(
                date="2026-03-09",
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc="2026-03-09T07:00:00Z",
                        duration_min=30.0,
                    ),
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc="2026-03-09T17:00:00Z",
                        duration_min=45.0,
                    ),
                ],
            ),
            DailySnapshot(
                date="2026-03-10",
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc="2026-03-10T07:00:00Z",
                        duration_min=30.0,
                    ),
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc="2026-03-10T17:00:00Z",
                        duration_min=45.0,
                    ),
                ],
            ),
            DailySnapshot(
                date="2026-03-11",
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc="2026-03-11T07:00:00Z",
                        duration_min=30.0,
                    ),
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc="2026-03-11T17:00:00Z",
                        duration_min=45.0,
                    ),
                ],
            ),
        ]
        summary = summarise(snaps)
        assert summary.run_count == 3
        assert summary.lift_count == 3
        assert summary.run_consistency_pct == 100.0
        assert summary.lift_consistency_pct == 100.0

    def test_sleep_averages(self, sample_snapshots: list[DailySnapshot]) -> None:
        """Sleep averages should be computed from days that have sleep data."""
        summary = summarise(sample_snapshots)
        # Only 2 of 7 days have sleep data (Mar 09 and Mar 10)
        assert summary.avg_sleep_total_h is not None
        assert summary.avg_sleep_total_h == round((7.4 + 7.0) / 2, 2)
        assert summary.avg_sleep_efficiency_pct is not None
        assert summary.avg_sleep_deep_h is not None
        assert summary.avg_sleep_rem_h is not None

    def test_sleep_none_when_no_data(self) -> None:
        """Weeks without any sleep data should have None sleep averages."""
        snaps = [DailySnapshot(date=f"2026-03-{9 + i:02d}") for i in range(7)]
        summary = summarise(snaps)
        assert summary.avg_sleep_total_h is None
        assert summary.avg_sleep_efficiency_pct is None
        assert summary.avg_sleep_deep_h is None

    def test_all_none_metrics(self) -> None:
        """A week of days with all metrics None should not crash."""
        snaps = [DailySnapshot(date=f"2026-03-{9 + i:02d}") for i in range(7)]
        summary = summarise(snaps)
        assert summary.run_count == 0
        assert summary.avg_resting_hr is None
        assert summary.avg_hrv_ms is None
        assert summary.avg_steps == 0
        assert summary.avg_recovery_index is None
        assert summary.hrv_trend is None

    def test_short_functional_warmup_does_not_increment_lift_count(self) -> None:
        snaps = [
            DailySnapshot(
                date="2026-03-09",
                workouts=[
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc="2026-03-09T17:00:00Z",
                        duration_min=45.0,
                    )
                ],
            ),
            DailySnapshot(
                date="2026-03-10",
                workouts=[
                    WorkoutSnapshot(
                        type="Functional Strength Training",
                        category="lift",
                        start_utc="2026-03-10T07:00:00Z",
                        duration_min=6.0,
                    )
                ],
            ),
        ]

        summary = summarise(snaps)

        assert summary.lift_count == 1
        assert summary.total_lift_min == 45.0

    def test_second_real_lift_updates_lift_count(self) -> None:
        snaps = [
            DailySnapshot(
                date="2026-03-09",
                workouts=[
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc="2026-03-09T17:00:00Z",
                        duration_min=45.0,
                    )
                ],
            ),
            DailySnapshot(
                date="2026-03-10",
                workouts=[
                    WorkoutSnapshot(
                        type="Functional Strength Training",
                        category="lift",
                        start_utc="2026-03-10T07:00:00Z",
                        duration_min=18.0,
                        hr_avg=108.0,
                    )
                ],
            ),
        ]

        summary = summarise(snaps)

        assert summary.lift_count == 2
        assert summary.total_lift_min == 63.0
        assert summary.avg_lift_hr == 108.0
