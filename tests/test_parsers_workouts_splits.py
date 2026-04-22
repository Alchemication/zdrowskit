"""Tests for per-kilometre split extraction in src/parsers/workouts.py."""

from __future__ import annotations

import json
import math
from pathlib import Path

from parsers.workouts import parse_workouts

_ONE_KM_LAT_DEG = 0.008993216059187304


def _write_workouts_file(path: Path, workout: dict) -> None:
    path.write_text(json.dumps({"data": {"workouts": [workout]}}), encoding="utf-8")


class TestParseWorkoutSplits:
    def test_emits_splits_for_synthetic_3km_route(self, tmp_path: Path) -> None:
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 960.0,
            "distance": {"qty": 3.0, "units": "km"},
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "altitude": 100.0,
                    "speed": 3.2,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "altitude": 110.0,
                    "speed": 3.4,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "altitude": 105.0,
                    "speed": 3.0,
                    "timestamp": "2026-03-10T07:10:30Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 3,
                    "longitude": 0.0,
                    "altitude": 120.0,
                    "speed": 2.6,
                    "timestamp": "2026-03-10T07:16:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        splits = workouts[0].splits
        assert len(splits) == 3
        assert [split.km_index for split in splits] == [1, 2, 3]
        assert splits[0].pace_min_km == 5.0
        assert splits[1].pace_min_km == 5.5
        assert splits[2].pace_min_km == 5.5
        assert splits[0].avg_speed_ms == 3.3
        assert splits[1].avg_speed_ms == 3.2
        assert splits[2].avg_speed_ms == 2.8
        assert splits[0].elevation_gain_m == 10.0
        assert splits[0].elevation_loss_m == 0.0
        assert splits[1].elevation_gain_m == 0.0
        assert splits[1].elevation_loss_m == 5.0
        assert splits[2].elevation_gain_m == 15.0

    def test_route_shorter_than_1km_emits_no_splits(self, tmp_path: Path) -> None:
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 180.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "speed": 3.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG / 2,
                    "longitude": 0.0,
                    "speed": 3.0,
                    "timestamp": "2026-03-10T07:03:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        assert workouts[0].splits == []

    def test_nan_route_point_is_ignored_without_crashing(self, tmp_path: Path) -> None:
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 600.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "speed": 3.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": math.nan,
                    "longitude": 0.0,
                    "speed": 3.0,
                    "timestamp": "2026-03-10T07:02:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "speed": 3.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        assert workouts[0].splits == []

    def test_missing_altitude_yields_null_split_elevation(self, tmp_path: Path) -> None:
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 300.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "speed": 3.1,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "speed": 3.3,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        assert len(workouts[0].splits) == 1
        assert workouts[0].splits[0].elevation_gain_m is None
        assert workouts[0].splits[0].elevation_loss_m is None
