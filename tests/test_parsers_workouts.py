"""Tests for src/parsers/workouts.py."""

from __future__ import annotations

from pathlib import Path

from parsers.workouts import _category, _counts_as_lift, _qty, parse_workouts


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
        assert _qty({"qty": 81.6, "units": "count/min"}) == 81.6

    def test_none_input(self) -> None:
        assert _qty(None) is None

    def test_missing_qty_key(self) -> None:
        assert _qty({"units": "count/min"}) is None


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
