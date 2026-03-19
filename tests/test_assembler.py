"""Tests for src/assembler.py."""

from __future__ import annotations

import json
from pathlib import Path

from models import WorkoutSnapshot
from assembler import _safe_float, _workout_date, assemble


class TestWorkoutDate:
    def test_extracts_date(self) -> None:
        w = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-03-10T07:00:00Z",
            duration_min=35.0,
        )
        assert _workout_date(w) == "2026-03-10"

    def test_different_dates(self) -> None:
        w = WorkoutSnapshot(
            type="Walk",
            category="walk",
            start_utc="2025-12-31T23:59:00Z",
            duration_min=20.0,
        )
        assert _workout_date(w) == "2025-12-31"


class TestSafeFloat:
    def test_with_int(self) -> None:
        assert _safe_float(5) == 5.0
        assert isinstance(_safe_float(5), float)

    def test_with_float(self) -> None:
        assert _safe_float(3.14) == 3.14

    def test_with_none(self) -> None:
        assert _safe_float(None) is None


class TestAssemble:
    def _write_metrics(self, data_dir: Path, filename: str, data: list[dict]) -> None:
        metrics_dir = data_dir / "Metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        (metrics_dir / filename).write_text(json.dumps({"data": {"metrics": data}}))

    def _write_workouts(self, data_dir: Path, workouts: list[dict]) -> None:
        workouts_dir = data_dir / "Workouts"
        workouts_dir.mkdir(parents=True, exist_ok=True)
        (workouts_dir / "workouts.json").write_text(
            json.dumps({"data": {"workouts": workouts}})
        )

    def test_empty_data_dir(self, tmp_path: Path) -> None:
        result = assemble(tmp_path)
        assert result == []

    def test_metrics_only(self, tmp_path: Path) -> None:
        self._write_metrics(
            tmp_path,
            "activity.json",
            [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [{"date": "2026-03-10 00:00:00 +0100", "qty": 9500}],
                }
            ],
        )
        result = assemble(tmp_path)
        assert len(result) == 1
        assert result[0].date == "2026-03-10"
        assert result[0].steps == 9500
        assert result[0].workouts == []

    def test_workouts_attached_to_correct_date(self, tmp_path: Path) -> None:
        self._write_metrics(
            tmp_path,
            "activity.json",
            [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [{"date": "2026-03-10 00:00:00 +0100", "qty": 9500}],
                }
            ],
        )
        self._write_workouts(
            tmp_path,
            [
                {
                    "name": "Outdoor Run",
                    "start": "2026-03-10 07:00:00 +0100",
                    "end": "2026-03-10 07:35:00 +0100",
                    "activeEnergy": {"qty": 214.7, "units": "kcal"},
                    "heartRateData": [
                        {
                            "qty": 120,
                            "units": "count/min",
                            "date": "2026-03-10 07:01:00 +0100",
                        },
                        {
                            "qty": 155,
                            "units": "count/min",
                            "date": "2026-03-10 07:15:00 +0100",
                        },
                        {
                            "qty": 178,
                            "units": "count/min",
                            "date": "2026-03-10 07:30:00 +0100",
                        },
                    ],
                }
            ],
        )
        result = assemble(tmp_path)
        assert len(result) == 1
        assert result[0].date == "2026-03-10"
        assert len(result[0].workouts) == 1
        assert result[0].workouts[0].category == "run"

    def test_recovery_index_computed(self, tmp_path: Path) -> None:
        self._write_metrics(
            tmp_path,
            "heart.json",
            [
                {
                    "name": "heart_rate_variability",
                    "units": "ms",
                    "data": [{"date": "2026-03-10 00:00:00 +0100", "qty": 60.0}],
                },
                {
                    "name": "resting_heart_rate",
                    "units": "count/min",
                    "data": [{"date": "2026-03-10 00:00:00 +0100", "qty": 50.0}],
                },
            ],
        )
        result = assemble(tmp_path)
        assert len(result) == 1
        assert result[0].recovery_index == round(60.0 / 50, 4)

    def test_workouts_without_matching_metrics(self, tmp_path: Path) -> None:
        """Workouts on a date with no metrics should still produce a DailySnapshot."""
        self._write_workouts(
            tmp_path,
            [
                {
                    "name": "Outdoor Run",
                    "start": "2026-03-10 07:00:00 +0100",
                    "end": "2026-03-10 07:35:00 +0100",
                    "activeEnergy": {"qty": 214.7, "units": "kcal"},
                }
            ],
        )
        result = assemble(tmp_path)
        assert len(result) == 1
        assert result[0].date == "2026-03-10"
        assert result[0].steps is None
        assert result[0].resting_hr is None
        assert len(result[0].workouts) == 1
        assert result[0].workouts[0].category == "run"

    def test_recovery_index_none_without_hrv(self, tmp_path: Path) -> None:
        self._write_metrics(
            tmp_path,
            "heart.json",
            [
                {
                    "name": "resting_heart_rate",
                    "units": "count/min",
                    "data": [{"date": "2026-03-10 00:00:00 +0100", "qty": 50.0}],
                },
            ],
        )
        result = assemble(tmp_path)
        assert result[0].recovery_index is None
