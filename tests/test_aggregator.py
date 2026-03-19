"""Tests for src/aggregator.py."""

from __future__ import annotations

from models import DailySnapshot, WorkoutSnapshot
from aggregator import _best_run_pace, _hrv_trend, _safe_mean, summarise


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
