"""Tests for src/report.py."""

from __future__ import annotations

from models import DailySnapshot
from report import current_week_bounds, fmt, group_by_week, ri_label, to_dict


class TestFmt:
    def test_none_returns_dash(self) -> None:
        assert fmt(None) == "\u2014"  # em-dash

    def test_float_with_unit(self) -> None:
        assert fmt(52.3, " bpm") == "52.3 bpm"

    def test_int_no_decimals(self) -> None:
        assert fmt(9500) == "9500"

    def test_float_custom_decimals(self) -> None:
        assert fmt(52.125, " bpm", decimals=2) == "52.12 bpm"


class TestRiLabel:
    def test_low(self) -> None:
        assert ri_label(0.5) == "low"
        assert ri_label(0.89) == "low"

    def test_normal(self) -> None:
        assert ri_label(0.9) == "normal"
        assert ri_label(1.0) == "normal"
        assert ri_label(1.5) == "normal"

    def test_high(self) -> None:
        assert ri_label(1.51) == "high"
        assert ri_label(2.0) == "high"


class TestCurrentWeekBounds:
    def test_sunday(self) -> None:
        # 2026-03-15 is a Sunday
        monday, sunday = current_week_bounds("2026-03-15")
        assert monday == "2026-03-09"
        assert sunday == "2026-03-15"

    def test_monday(self) -> None:
        monday, sunday = current_week_bounds("2026-03-09")
        assert monday == "2026-03-09"
        assert sunday == "2026-03-15"

    def test_midweek(self) -> None:
        monday, sunday = current_week_bounds("2026-03-12")
        assert monday == "2026-03-09"
        assert sunday == "2026-03-15"


class TestGroupByWeek:
    def test_two_weeks(self) -> None:
        snapshots = [
            DailySnapshot(date=f"2026-03-{d:02d}")
            for d in range(9, 23)  # 14 days = 2 weeks
        ]
        groups = group_by_week(snapshots)
        assert len(groups) == 2
        assert len(groups[0]) == 7
        assert len(groups[1]) == 7

    def test_single_day(self) -> None:
        groups = group_by_week([DailySnapshot(date="2026-03-12")])
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_empty(self) -> None:
        assert group_by_week([]) == []


class TestToDict:
    def test_dataclass_round_trip(self) -> None:
        snap = DailySnapshot(date="2026-03-10", steps=9500, resting_hr=52)
        d = to_dict(snap)
        assert isinstance(d, dict)
        assert d["date"] == "2026-03-10"
        assert d["steps"] == 9500
        assert d["resting_hr"] == 52

    def test_nested_workouts(self, sample_workout_run) -> None:
        snap = DailySnapshot(date="2026-03-10", workouts=[sample_workout_run])
        d = to_dict(snap)
        assert len(d["workouts"]) == 1
        assert d["workouts"][0]["type"] == "Outdoor Run"
