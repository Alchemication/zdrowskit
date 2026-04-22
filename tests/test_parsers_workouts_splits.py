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

    def test_swim_route_emits_no_splits(self, tmp_path: Path) -> None:
        """Open Water Swim routes carry unreliable GPS and must not emit splits."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Open Water Swim",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 600.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "speed": 1.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "speed": 1.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        assert workouts[0].splits == []

    def test_multi_km_gps_dropout_segment_is_skipped(self, tmp_path: Path) -> None:
        """A multi-km segment with plausible speed but absurd distance is skipped."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 1800.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                # Apparent 3 km "run" in 7 minutes (~7 m/s) — passes the speed
                # cap but obviously a GPS dropout where sampling resumed.
                {
                    "latitude": _ONE_KM_LAT_DEG * 3,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:07:00Z",
                },
                # Normal 5:00/km pace resumes.
                {
                    "latitude": _ONE_KM_LAT_DEG * 4,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:12:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 5,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:17:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        splits = workouts[0].splits
        assert len(splits) == 2
        assert all(split.pace_min_km > 3.0 for split in splits)

    def test_gps_glitch_segment_is_skipped(self, tmp_path: Path) -> None:
        """A single teleport segment must not yield a phantom sub-elite split."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 900.0,
            "route": [
                # Km 1: normal 5:00/km pace (3.33 m/s).
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "speed": 3.33,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "speed": 3.33,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
                # GPS teleport: 2 km in 10 seconds → 200 m/s (way above the run cap).
                {
                    "latitude": _ONE_KM_LAT_DEG * 3,
                    "longitude": 0.0,
                    "speed": 3.33,
                    "timestamp": "2026-03-10T07:05:10Z",
                },
                # Km 2 resumes at 5:00/km pace after the glitch.
                {
                    "latitude": _ONE_KM_LAT_DEG * 4,
                    "longitude": 0.0,
                    "speed": 3.33,
                    "timestamp": "2026-03-10T07:10:10Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        splits = workouts[0].splits
        # Only the two real kilometres should show up; neither inherits the
        # glitch pace.
        assert len(splits) == 2
        assert all(split.pace_min_km > 3.0 for split in splits)

    def test_per_split_elevation_normalises_to_session_total(
        self, tmp_path: Path
    ) -> None:
        """Raw per-sample altitude is rescaled so splits sum to the session total."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 600.0,
            "elevationUp": {"qty": 10.0, "units": "m"},
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "altitude": 100.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                # Noisy intermediate point — 30 m of phantom climb in 500 m.
                {
                    "latitude": _ONE_KM_LAT_DEG / 2,
                    "longitude": 0.0,
                    "altitude": 130.0,
                    "timestamp": "2026-03-10T07:02:30Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "altitude": 110.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "altitude": 110.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        splits = workouts[0].splits
        assert len(splits) == 2
        total_gain = sum(split.elevation_gain_m or 0.0 for split in splits)
        assert math.isclose(total_gain, 10.0, abs_tol=0.05)
