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
        assert workouts[0].location_lat == 0.0
        assert workouts[0].location_lon == 0.0
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


def _hr_bin(minute: int, avg: float, maximum: float) -> dict:
    """Build one 60-second heart-rate bin starting at 07:0{minute}:00Z."""
    return {
        "date": f"2026-03-10 07:{minute:02d}:00 +0000",
        "units": "count/min",
        "Min": avg,
        "Avg": avg,
        "Max": maximum,
    }


class TestSplitHeartRate:
    def test_time_weights_bins_across_km_boundary(self, tmp_path: Path) -> None:
        """A bin straddling a boundary contributes to both splits in proportion."""
        path = tmp_path / "workouts.json"
        # Two 5-minute kilometres. Bins 0-4 cover km 1, bins 5-9 cover km 2.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 600.0,
            "heartRateData": [_hr_bin(m, 140.0, 145.0) for m in range(5)]
            + [_hr_bin(m, 160.0, 170.0) for m in range(5, 10)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 2
        assert splits[0].hr_avg == 140.0
        assert splits[0].hr_max == 145
        assert splits[0].hr_coverage == 1.0
        assert splits[1].hr_avg == 160.0
        assert splits[1].hr_max == 170
        assert splits[1].hr_coverage == 1.0

    def test_partial_bin_overlap_is_weighted_not_counted(self, tmp_path: Path) -> None:
        """Half a split at 120 and half at 180 averages to 150, not to a bin count."""
        path = tmp_path / "workouts.json"
        # One 4-minute kilometre: two bins at 120 bpm, then two at 180 bpm.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 240.0,
            "heartRateData": [
                _hr_bin(0, 120.0, 125.0),
                _hr_bin(1, 120.0, 125.0),
                _hr_bin(2, 180.0, 185.0),
                _hr_bin(3, 180.0, 185.0),
            ],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:04:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 1
        assert splits[0].hr_avg == 150.0
        assert splits[0].hr_max == 185
        assert splits[0].hr_coverage == 1.0

    def test_coverage_below_floor_nulls_hr_but_keeps_coverage(
        self, tmp_path: Path
    ) -> None:
        """A thinly sampled km reports no heart rate, and says how thin it was."""
        path = tmp_path / "workouts.json"
        # A 10-minute kilometre with only the first 2 minutes sampled: 0.2.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 600.0,
            "heartRateData": [_hr_bin(0, 130.0, 135.0), _hr_bin(1, 130.0, 135.0)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 1
        assert splits[0].hr_avg is None
        assert splits[0].hr_max is None
        assert splits[0].hr_coverage == 0.2
        # The pace for that km is still known and must survive the HR gap.
        assert splits[0].pace_min_km == 10.0

    def test_sampling_dropout_does_not_smear_one_bin(self, tmp_path: Path) -> None:
        """A bin before a 5-minute gap covers 60 s, not the gap it precedes."""
        path = tmp_path / "workouts.json"
        # 6-minute km; bins at minute 0 and minute 5 only -> 2 of 6 minutes.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 360.0,
            "heartRateData": [_hr_bin(0, 130.0, 135.0), _hr_bin(5, 150.0, 155.0)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:06:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 1
        assert splits[0].hr_coverage == round(2 / 6, 4)
        assert splits[0].hr_avg is None

    def test_workout_without_hr_data_reports_zero_coverage(
        self, tmp_path: Path
    ) -> None:
        """No heartRateData yields null HR and 0.0 coverage, not a crash."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 300.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 1
        assert splits[0].hr_avg is None
        assert splits[0].hr_max is None
        assert splits[0].hr_coverage == 0.0

    def test_malformed_hr_entries_are_dropped(self, tmp_path: Path) -> None:
        """Unparseable dates, missing averages, and non-positive bpm are ignored."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 300.0,
            "heartRateData": [
                {"date": "not-a-date", "Avg": 150.0},
                {"date": "2026-03-10 07:01:00 +0000"},
                {"date": "2026-03-10 07:02:00 +0000", "Avg": 0},
                "junk",
                _hr_bin(0, 140.0, 145.0),
                _hr_bin(1, 140.0, 145.0),
                _hr_bin(2, 140.0, 145.0),
                _hr_bin(3, 140.0, 145.0),
                _hr_bin(4, 140.0, 145.0),
            ],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 1
        assert splits[0].hr_avg == 140.0
        assert splits[0].hr_coverage == 1.0


def _step_bin(minute: int, steps: float) -> dict:
    """Build one 60-second step-count bin starting at 07:0{minute}:00Z."""
    return {
        "date": f"2026-03-10 07:{minute:02d}:00 +0000",
        "units": "count",
        "qty": steps,
    }


class TestSplitCadence:
    def test_cadence_from_step_bins(self, tmp_path: Path) -> None:
        """Steps per bin become steps per minute over the split."""
        path = tmp_path / "workouts.json"
        # 5-minute km, 165 steps in each of 5 bins -> 165 spm.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 300.0,
            "stepCount": [_step_bin(m, 165.0) for m in range(5)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 1
        assert splits[0].cadence_spm == 165.0
        assert splits[0].cadence_coverage == 1.0
        # Stride is derived, not stored: 1000 / (cadence * pace).
        stride_m = 1000 / (splits[0].cadence_spm * splits[0].pace_min_km)
        assert math.isclose(stride_m, 1.2121, abs_tol=0.001)

    def test_bin_straddling_boundary_splits_its_steps(self, tmp_path: Path) -> None:
        """A step bin is a total, so an overlap takes its share, not all of it."""
        path = tmp_path / "workouts.json"
        # Two 3.5-minute kilometres, so the bin at minute 3 straddles the
        # boundary at 210 s. That bin carries a surge of 300 steps against 180
        # elsewhere: counting it whole into both splits would read 240 spm.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 420.0,
            "stepCount": [_step_bin(m, 300.0 if m == 3 else 180.0) for m in range(7)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:03:30Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:07:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert len(splits) == 2
        # Each split takes half the surge bin: (3*180 + 150) / 3.5 min.
        assert splits[0].cadence_spm == 197.1
        assert splits[1].cadence_spm == 197.1
        assert splits[0].cadence_coverage == 1.0
        assert splits[1].cadence_coverage == 1.0

    def test_partial_coverage_nulls_cadence_but_keeps_coverage(
        self, tmp_path: Path
    ) -> None:
        """Cadence is withheld on the same terms as heart rate."""
        path = tmp_path / "workouts.json"
        # 10-minute km with 2 minutes of step samples: coverage 0.2.
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 600.0,
            "stepCount": [_step_bin(0, 150.0), _step_bin(1, 150.0)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert splits[0].cadence_spm is None
        assert splits[0].cadence_coverage == 0.2

    def test_hr_and_cadence_coverage_are_independent(self, tmp_path: Path) -> None:
        """One series dropping out must not withhold the other."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 300.0,
            "heartRateData": [_hr_bin(m, 150.0, 155.0) for m in range(5)],
            "stepCount": [_step_bin(0, 160.0)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert splits[0].hr_avg == 150.0
        assert splits[0].hr_coverage == 1.0
        assert splits[0].cadence_spm is None
        assert splits[0].cadence_coverage == 0.2

    def test_workout_without_step_data_reports_zero_coverage(
        self, tmp_path: Path
    ) -> None:
        """No stepCount yields null cadence and 0.0 coverage, not a crash."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 300.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:05:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        assert splits[0].cadence_spm is None
        assert splits[0].cadence_coverage == 0.0


class TestWalkSplits:
    def test_outdoor_walk_emits_splits(self, tmp_path: Path) -> None:
        """Walks are route-bearing and now produce splits like runs."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Walk",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 1200.0,
            "heartRateData": [_hr_bin(m, 100.0, 110.0) for m in range(20)],
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG * 2,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:20:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert workouts[0].category == "walk"
        splits = workouts[0].splits
        assert [split.km_index for split in splits] == [1, 2]
        assert splits[0].pace_min_km == 10.0
        assert splits[0].hr_avg == 100.0

    def test_hiking_maps_to_walk_and_emits_splits(self, tmp_path: Path) -> None:
        """Hiking is ambulatory with usable route data, so it splits as a walk."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Hiking",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 900.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:15:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        workouts = parse_workouts(path)

        assert workouts[0].category == "walk"
        assert [split.km_index for split in workouts[0].splits] == [1]

    def test_hiit_gets_its_own_category(self, tmp_path: Path) -> None:
        """Left in "other" a HIIT habit is invisible: nothing reads that bucket."""
        path = tmp_path / "workouts.json"
        _write_workouts_file(
            path,
            {
                "name": "High Intensity Interval Training",
                "start": "2026-03-10 18:00:00 +0000",
                "duration": 1500.0,
            },
        )

        workouts = parse_workouts(path)

        assert workouts[0].category == "hiit"
        assert workouts[0].counts_as_lift is False
        assert workouts[0].splits == []

    def test_paddle_sports_stays_uncategorised(self, tmp_path: Path) -> None:
        """Paddling is reached by naming the type, not by inventing a category."""
        path = tmp_path / "workouts.json"
        _write_workouts_file(
            path,
            {
                "name": "Paddle Sports",
                "start": "2026-03-10 16:00:00 +0000",
                "duration": 3600.0,
            },
        )

        workouts = parse_workouts(path)

        assert workouts[0].category == "other"
        assert workouts[0].type == "Paddle Sports"

    def test_walk_gps_glitch_segment_is_skipped(self, tmp_path: Path) -> None:
        """The walk speed cap rejects a teleport that a run cap would also reject."""
        path = tmp_path / "workouts.json"
        workout = {
            "name": "Outdoor Walk",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 1200.0,
            "route": [
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:00Z",
                },
                # 500 m in 1 s — far above any real walking segment.
                {
                    "latitude": _ONE_KM_LAT_DEG / 2,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:00:01Z",
                },
                {
                    "latitude": _ONE_KM_LAT_DEG,
                    "longitude": 0.0,
                    "timestamp": "2026-03-10T07:10:00Z",
                },
            ],
        }
        _write_workouts_file(path, workout)

        splits = parse_workouts(path)[0].splits

        # The glitch segment is dropped, leaving under 1 km of accepted route.
        assert splits == []


class TestCategoryMapping:
    def test_apple_gerund_names_map_to_cycle(self, tmp_path: Path) -> None:
        """Auto Export writes "Outdoor Cycling", which must not fall to "other"."""
        path = tmp_path / "workouts.json"
        for name in ("Outdoor Cycling", "Indoor Cycling", "Outdoor Cycle"):
            _write_workouts_file(
                path,
                {
                    "name": name,
                    "start": "2026-03-10 07:00:00 +0000",
                    "duration": 600.0,
                },
            )
            assert parse_workouts(path)[0].category == "cycle", name

    def test_bare_run_name_maps_to_run(self, tmp_path: Path) -> None:
        """Some sources write a bare "Run" rather than "Outdoor Run"."""
        path = tmp_path / "workouts.json"
        _write_workouts_file(
            path,
            {
                "name": "Run",
                "start": "2026-03-10 07:00:00 +0000",
                "duration": 600.0,
            },
        )

        assert parse_workouts(path)[0].category == "run"
