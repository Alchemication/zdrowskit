"""Tests for src/parsers/workouts.py."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from models import WorkoutSnapshot
from parsers.normalise import UnitNormaliser
from parsers.workouts import _category, _counts_as_lift, parse_workouts


class TestCategory:
    def test_outdoor_run(self) -> None:
        assert _category("Outdoor Run") == "run"

    def test_indoor_run(self) -> None:
        assert _category("Indoor Run") == "run"

    def test_strength_training(self) -> None:
        assert _category("Traditional Strength Training") == "lift"

    def test_outdoor_walk(self) -> None:
        assert _category("Outdoor Walk") == "walk"

    def test_outdoor_cycle(self) -> None:
        assert _category("Outdoor Cycle") == "cycle"

    def test_unknown_defaults_to_other(self) -> None:
        assert _category("Yoga") == "other"
        assert _category("Swimming") == "other"


class TestQty:
    def test_extracts_value(self) -> None:
        obj = {"qty": 81.6, "units": "count/min"}
        assert UnitNormaliser().qty(obj, "count/min", field="hr") == 81.6

    def test_none_input(self) -> None:
        assert UnitNormaliser().qty(None, "count/min", field="hr") is None

    def test_missing_qty_key(self) -> None:
        obj = {"units": "count/min"}
        assert UnitNormaliser().qty(obj, "count/min", field="hr") is None


class TestCountsAsLift:
    def test_traditional_strength_always_counts(self) -> None:
        assert _counts_as_lift("Traditional Strength Training", 5.0) is True

    def test_short_functional_strength_does_not_count(self) -> None:
        assert _counts_as_lift("Functional Strength Training", 14.9) is False

    def test_functional_strength_at_threshold_counts(self) -> None:
        assert _counts_as_lift("Functional Strength Training", 15.0) is True


class TestParseWorkouts:
    def test_count_and_order(self, fixtures_dir: Path) -> None:
        workouts = parse_workouts(fixtures_dir / "workouts.json")
        assert len(workouts) == 3
        # Should be sorted by start_utc
        assert workouts[0].category == "lift"
        assert workouts[1].category == "run"
        assert workouts[2].category == "other"  # Yoga

    def test_duration_converted_to_minutes(self, fixtures_dir: Path) -> None:
        workouts = parse_workouts(fixtures_dir / "workouts.json")
        run = [w for w in workouts if w.category == "run"][0]
        assert run.duration_min == 2100.0 / 60.0  # 35.0

    def test_nested_hr_extraction(self, fixtures_dir: Path) -> None:
        workouts = parse_workouts(fixtures_dir / "workouts.json")
        run = [w for w in workouts if w.category == "run"][0]
        assert run.hr_avg == 155.0
        assert run.hr_min == 120
        assert run.hr_max == 178

    def test_optional_fields_none_when_missing(self, fixtures_dir: Path) -> None:
        workouts = parse_workouts(fixtures_dir / "workouts.json")
        yoga = [w for w in workouts if w.type == "Yoga"][0]
        assert yoga.temperature_c is None
        assert yoga.humidity_pct is None
        assert yoga.hr_avg is None

    def test_temperature_and_humidity(self, fixtures_dir: Path) -> None:
        workouts = parse_workouts(fixtures_dir / "workouts.json")
        run = [w for w in workouts if w.category == "run"][0]
        assert run.temperature_c == 8.0
        assert run.humidity_pct == 65

    def test_filters_out_sub_minute_workout(self, tmp_path: Path) -> None:
        path = tmp_path / "workouts.json"
        path.write_text(
            """
            {
              "data": {
                "workouts": [
                  {
                    "name": "Functional Strength Training",
                    "start": "2026-03-10 07:00:00 +0000",
                    "duration": 30
                  },
                  {
                    "name": "Outdoor Run",
                    "start": "2026-03-10 07:02:00 +0000",
                    "duration": 1200
                  }
                ]
              }
            }
            """.strip()
        )

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        assert workouts[0].type == "Outdoor Run"

    def test_short_functional_strength_is_kept_but_not_counted(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "workouts.json"
        path.write_text(
            """
            {
              "data": {
                "workouts": [
                  {
                    "name": "Functional Strength Training",
                    "start": "2026-03-10 07:00:00 +0000",
                    "duration": 600
                  }
                ]
              }
            }
            """.strip()
        )

        workouts = parse_workouts(path)

        assert len(workouts) == 1
        assert workouts[0].category == "lift"
        assert workouts[0].counts_as_lift is False


class TestImperialWorkouts:
    @pytest.fixture
    def run(self, fixtures_dir: Path) -> WorkoutSnapshot:
        return parse_workouts(fixtures_dir / "imperial_workouts.json")[0]

    def test_distance_miles(self, run: WorkoutSnapshot) -> None:
        assert run.gpx_distance_km == pytest.approx(3.1 * 1.609344, abs=1e-3)

    def test_speed_mph_to_ms(self, run: WorkoutSnapshot) -> None:
        assert run.gpx_avg_speed_ms == pytest.approx(6.0 * 0.44704, abs=1e-4)

    def test_elevation_feet(self, run: WorkoutSnapshot) -> None:
        assert run.gpx_elevation_gain_m == pytest.approx(20 * 0.3048)

    def test_energy_kcal(self, run: WorkoutSnapshot) -> None:
        assert run.active_energy_kj == pytest.approx(500 * 4.184)

    def test_temperature_degf(self, run: WorkoutSnapshot) -> None:
        assert run.temperature_c == pytest.approx(10.0)

    def test_intensity_met(self, run: WorkoutSnapshot) -> None:
        assert run.intensity_kcal_per_hr_kg == 8.0

    def test_heart_rate_bpm(self, run: WorkoutSnapshot) -> None:
        assert (run.hr_min, run.hr_avg, run.hr_max) == (120, 150.0, 170)

    def test_split_elevation_rescaled_to_converted_gain(
        self, run: WorkoutSnapshot
    ) -> None:
        assert len(run.splits) == 1
        assert run.splits[0].elevation_gain_m == pytest.approx(
            run.gpx_elevation_gain_m, abs=0.01
        )

    def test_bpm_bins_feed_split_heart_rate(self, run: WorkoutSnapshot) -> None:
        assert run.splits[0].hr_avg == 150.0
        assert run.splits[0].cadence_spm == 170.0


class TestWorkoutUnknownUnits:
    def _parse(self, tmp_path: Path, **fields: object) -> WorkoutSnapshot:
        workout = {
            "name": "Outdoor Run",
            "start": "2026-03-10 07:00:00 +0000",
            "duration": 1800.0,
            **fields,
        }
        path = tmp_path / "workouts.json"
        path.write_text(json.dumps({"data": {"workouts": [workout]}}))
        return parse_workouts(path)[0]

    def test_unknown_temperature_unit_is_dropped_and_warned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            run = self._parse(tmp_path, temperature={"qty": 50, "units": "furlong"})
        assert run.temperature_c is None
        assert len(caplog.records) == 1
        assert "furlong" in caplog.records[0].getMessage()

    def test_distance_without_units_is_dropped(self, tmp_path: Path) -> None:
        assert self._parse(tmp_path, distance={"qty": 5.0}).gpx_distance_km is None

    def test_energy_without_units_falls_back_to_zero(self, tmp_path: Path) -> None:
        run = self._parse(tmp_path, activeEnergyBurned={"qty": 500})
        assert run.active_energy_kj == 0.0

    def test_heart_rate_bin_with_unknown_unit_is_kept_and_warned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lat = 0.008993216059187304 / 1000 * 150
        route = [
            {
                "latitude": lat * i,
                "longitude": 0.0,
                "altitude": 100.0,
                "speed": 3.3,
                "timestamp": f"2026-03-10T07:{(45 * i) // 60:02d}:{(45 * i) % 60:02d}Z",
            }
            for i in range(9)
        ]
        bins = [
            {
                "date": f"2026-03-10 07:{m:02d}:00 +0000",
                "Avg": 150,
                "Max": 160,
                "units": "beats",
            }
            for m in range(7)
        ]
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            run = self._parse(tmp_path, route=route, heartRateData=bins)
        assert len(run.splits) == 1
        assert run.splits[0].hr_avg == 150.0
        assert len(caplog.records) == 1
        assert "beats" in caplog.records[0].getMessage()

    @pytest.mark.parametrize("bad", [65, "65", [65]])
    def test_non_dict_field_values_do_not_abort_the_payload(
        self, tmp_path: Path, bad: object
    ) -> None:
        run = self._parse(
            tmp_path,
            humidity=bad,
            temperature=bad,
            distance=bad,
            heartRate={"avg": bad},
        )
        assert run.humidity_pct is None
        assert run.temperature_c is None
        assert run.gpx_distance_km is None
        assert run.hr_avg is None
