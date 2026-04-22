"""Tests for deterministic feel multipliers applied to manual activities."""

from __future__ import annotations

import pytest

from feel_adjust import apply_sleep_feel, apply_workout_feel


class TestApplyWorkoutFeel:
    def test_none_feel_is_passthrough(self) -> None:
        clone = {"hr_avg": 150.0, "active_energy_kj": 1800.0, "source_note": "x"}
        adjusted, flag = apply_workout_feel(clone, None)
        assert adjusted == clone
        assert flag is False

    def test_solid_feel_is_noop(self) -> None:
        clone = {"hr_avg": 150.0, "active_energy_kj": 1800.0, "source_note": "x"}
        adjusted, flag = apply_workout_feel(clone, "solid")
        assert adjusted == clone
        assert flag is False

    def test_easy_reduces_hr_energy_speed(self) -> None:
        clone = {
            "hr_avg": 150.0,
            "hr_max": 170,
            "active_energy_kj": 1800.0,
            "gpx_avg_speed_ms": 3.0,
            "gpx_distance_km": 5.0,
            "source_note": "cloned from Apr 1 run",
        }
        adjusted, flag = apply_workout_feel(clone, "easy")
        assert flag is True
        assert adjusted["hr_avg"] == pytest.approx(150.0 * 0.95, abs=0.1)
        assert adjusted["hr_max"] == round(170 * 0.95)
        assert adjusted["active_energy_kj"] == pytest.approx(1800.0 * 0.92, abs=0.1)
        assert adjusted["gpx_avg_speed_ms"] == pytest.approx(3.0 * 0.92, abs=0.01)
        # Distance tracks speed when duration is fixed.
        assert adjusted["gpx_distance_km"] == pytest.approx(5.0 * 0.92, abs=0.01)
        assert "easy" in adjusted["source_note"]

    def test_hard_increases_hr_energy_speed(self) -> None:
        clone = {"hr_avg": 150.0, "active_energy_kj": 1800.0, "gpx_avg_speed_ms": 3.0}
        adjusted, flag = apply_workout_feel(clone, "hard")
        assert flag is True
        assert adjusted["hr_avg"] == pytest.approx(150.0 * 1.06, abs=0.1)
        assert adjusted["active_energy_kj"] == pytest.approx(1800.0 * 1.08, abs=0.1)
        assert adjusted["gpx_avg_speed_ms"] == pytest.approx(3.0 * 1.05, abs=0.01)

    def test_wrecked_raises_hr_but_lowers_energy(self) -> None:
        clone = {"hr_avg": 150.0, "active_energy_kj": 1800.0, "gpx_avg_speed_ms": 3.0}
        adjusted, flag = apply_workout_feel(clone, "wrecked")
        assert flag is True
        assert adjusted["hr_avg"] == pytest.approx(150.0 * 1.03, abs=0.1)
        assert adjusted["active_energy_kj"] == pytest.approx(1800.0 * 0.97, abs=0.1)
        # Speed stays unchanged — you suffered more, didn't go faster.
        assert adjusted["gpx_avg_speed_ms"] == pytest.approx(3.0, abs=0.01)
        assert "RPE" in adjusted["source_note"]

    def test_none_fields_are_skipped(self) -> None:
        clone = {"hr_avg": None, "active_energy_kj": 1800.0, "gpx_distance_km": None}
        adjusted, flag = apply_workout_feel(clone, "easy")
        assert flag is True
        assert adjusted["hr_avg"] is None
        assert adjusted["gpx_distance_km"] is None
        assert adjusted["active_energy_kj"] == pytest.approx(1800.0 * 0.92, abs=0.1)

    def test_does_not_mutate_input(self) -> None:
        clone = {"hr_avg": 150.0, "active_energy_kj": 1800.0}
        apply_workout_feel(clone, "hard")
        assert clone["hr_avg"] == 150.0
        assert clone["active_energy_kj"] == 1800.0

    def test_source_note_suffix_appended(self) -> None:
        clone = {"hr_avg": 150.0, "source_note": "cloned from Apr 1"}
        adjusted, _ = apply_workout_feel(clone, "hard")
        assert adjusted["source_note"].startswith("cloned from Apr 1")
        assert "hard" in adjusted["source_note"]

    def test_source_note_starts_fresh_when_missing(self) -> None:
        clone = {"hr_avg": 150.0}
        adjusted, _ = apply_workout_feel(clone, "easy")
        assert adjusted["source_note"] == "adjusted down for 'easy' feel"

    def test_unknown_feel_is_noop(self) -> None:
        clone = {"hr_avg": 150.0}
        adjusted, flag = apply_workout_feel(clone, "whatever")
        assert adjusted == clone
        assert flag is False


class TestApplySleepFeel:
    def test_none_feel_uses_legacy_default(self) -> None:
        in_bed, flag = apply_sleep_feel(7.0, None)
        assert in_bed == pytest.approx(7.0 * 1.08, abs=0.01)
        assert flag is False

    def test_ok_feel_matches_legacy_default(self) -> None:
        in_bed, flag = apply_sleep_feel(7.0, "ok")
        assert in_bed == pytest.approx(7.0 * 1.08, abs=0.01)
        # "ok" matches default so we don't mark it as adjusted.
        assert flag is False

    def test_solid_tightens_in_bed(self) -> None:
        in_bed, flag = apply_sleep_feel(6.0, "solid")
        assert in_bed == pytest.approx(6.0 * 1.03, abs=0.01)
        assert flag is True

    def test_restless_pads_in_bed(self) -> None:
        in_bed, flag = apply_sleep_feel(7.0, "restless")
        assert in_bed == pytest.approx(7.0 * 1.15, abs=0.01)
        assert flag is True

    def test_wrecked_pads_the_most(self) -> None:
        in_bed, flag = apply_sleep_feel(7.0, "wrecked")
        assert in_bed == pytest.approx(7.0 * 1.25, abs=0.01)
        assert flag is True

    def test_unknown_feel_falls_back_to_default(self) -> None:
        in_bed, flag = apply_sleep_feel(7.0, "whatever")
        assert in_bed == pytest.approx(7.0 * 1.08, abs=0.01)
        assert flag is False
