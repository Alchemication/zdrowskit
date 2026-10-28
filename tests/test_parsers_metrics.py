"""Tests for src/parsers/metrics.py."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from parsers.metrics import (
    _parse_date,
    parse_all_metrics,
    parse_metrics_file,
    parse_metrics_payload,
)


class TestParseDate:
    def test_standard_format(self) -> None:
        assert _parse_date("2026-03-13 00:00:00 +0000") == "2026-03-13"

    def test_strips_time_portion(self) -> None:
        assert _parse_date("2025-01-01 12:34:56 +0100") == "2025-01-01"


class TestParseMetricsFile:
    def test_activity_fields(self, fixtures_dir: Path) -> None:
        result = parse_metrics_file(fixtures_dir / "activity.json")
        day = result["2026-03-09"]
        assert day["steps"] == 9500.0
        assert day["active_energy_kj"] == 1800.0
        assert day["exercise_min"] == 30.0

    def test_heart_rate_min_max_special_case(self, fixtures_dir: Path) -> None:
        """heart_rate uses Min/Max instead of qty — the trickiest parsing logic."""
        result = parse_metrics_file(fixtures_dir / "heart.json")
        day = result["2026-03-09"]
        assert day["hr_day_min"] == 48.0
        assert day["hr_day_max"] == 165.0

    def test_hrv_and_resting_hr(self, fixtures_dir: Path) -> None:
        result = parse_metrics_file(fixtures_dir / "heart.json")
        day = result["2026-03-10"]
        assert day["resting_hr"] == 54.0
        assert day["hrv_ms"] == 55.0
        assert day["vo2max"] == 45.2

    def test_unknown_metric_ignored(self, fixtures_dir: Path) -> None:
        """Metrics not in METRIC_MAP should be silently skipped."""
        result = parse_metrics_file(fixtures_dir / "activity.json")
        for day_data in result.values():
            assert "totally_unknown_metric" not in day_data

    def test_missing_qty_skipped(self, tmp_path: Path) -> None:
        """Entries without a 'qty' key should be skipped without error."""
        import json

        data = {
            "data": {
                "metrics": [
                    {
                        "name": "step_count",
                        "units": "count",
                        "data": [
                            {"date": "2026-03-09 00:00:00 +0000"},
                            {"date": "2026-03-10 00:00:00 +0000", "qty": 5000},
                        ],
                    }
                ]
            }
        }
        path = tmp_path / "test.json"
        path.write_text(json.dumps(data))
        result = parse_metrics_file(path)
        assert "2026-03-09" not in result
        assert result["2026-03-10"]["steps"] == 5000.0


class TestOvernightMetrics:
    """Respiratory rate and wrist temperature attach to the night, not the date."""

    def _parse(self, tmp_path: Path, metrics: list[dict]) -> dict:
        import json

        path = tmp_path / "overnight.json"
        path.write_text(json.dumps({"data": {"metrics": metrics}}))
        return parse_metrics_file(path)

    def test_zero_hour_sleep_entry_is_not_stored(self, tmp_path: Path) -> None:
        """An untracked night arrives as zero hours and must stay missing."""
        result = self._parse(
            tmp_path,
            [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-03-10 07:00:00 +0000",
                            "sleepStart": "2026-03-09 23:30:00 +0000",
                            "sleepEnd": "2026-03-10 07:00:00 +0000",
                            "totalSleep": 0.0,
                            "deep": 0.0,
                            "core": 0.0,
                            "rem": 0.0,
                            "awake": 0.0,
                        }
                    ],
                }
            ],
        )

        assert "sleep_total_h" not in result.get("2026-03-09", {})

    def test_samples_either_side_of_midnight_share_one_night(
        self, tmp_path: Path
    ) -> None:
        """A 23:00 and an 06:00 reading belong to the same night-start date."""
        result = self._parse(
            tmp_path,
            [
                {
                    "name": "respiratory_rate",
                    "units": "count/min",
                    "data": [
                        {"date": "2026-03-09 23:00:00 +0000", "qty": 15.0},
                        {"date": "2026-03-10 06:00:00 +0000", "qty": 17.0},
                    ],
                }
            ],
        )

        assert result["2026-03-09"]["respiratory_rate"] == 16.0
        assert "2026-03-10" not in result

    def test_night_readings_are_averaged_not_last_wins(self, tmp_path: Path) -> None:
        """The stored value is the night's mean, unlike the generic qty path."""
        result = self._parse(
            tmp_path,
            [
                {
                    "name": "apple_sleeping_wrist_temperature",
                    "units": "degC",
                    "data": [
                        {"date": "2026-03-10 01:00:00 +0000", "qty": 36.0},
                        {"date": "2026-03-10 02:00:00 +0000", "qty": 36.5},
                        {"date": "2026-03-10 03:00:00 +0000", "qty": 37.0},
                    ],
                }
            ],
        )

        assert result["2026-03-09"]["sleeping_wrist_temp_c"] == 36.5

    def test_aligns_with_sleep_night_attribution(self, tmp_path: Path) -> None:
        """Sleep and the overnight metrics land on the same row for one night."""
        result = self._parse(
            tmp_path,
            [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-03-10 07:00:00 +0000",
                            "sleepStart": "2026-03-09 23:30:00 +0000",
                            "sleepEnd": "2026-03-10 07:00:00 +0000",
                            "totalSleep": 7.0,
                            "deep": 1.0,
                            "core": 4.0,
                            "rem": 2.0,
                            "awake": 0.5,
                        }
                    ],
                },
                {
                    "name": "respiratory_rate",
                    "units": "count/min",
                    "data": [
                        {"date": "2026-03-09 23:45:00 +0000", "qty": 14.0},
                        {"date": "2026-03-10 05:00:00 +0000", "qty": 16.0},
                    ],
                },
                {
                    "name": "apple_sleeping_wrist_temperature",
                    "units": "degC",
                    "data": [{"date": "2026-03-10 02:00:00 +0000", "qty": 36.4}],
                },
            ],
        )

        night = result["2026-03-09"]
        assert night["sleep_total_h"] == 7.0
        assert night["respiratory_rate"] == 15.0
        assert night["sleeping_wrist_temp_c"] == 36.4

    def test_unparseable_date_is_dropped(self, tmp_path: Path) -> None:
        """A malformed timestamp drops that reading rather than raising."""
        result = self._parse(
            tmp_path,
            [
                {
                    "name": "respiratory_rate",
                    "units": "count/min",
                    "data": [
                        {"date": "not-a-date", "qty": 99.0},
                        {"date": "2026-03-10 02:00:00 +0000", "qty": 15.0},
                    ],
                }
            ],
        )

        assert result["2026-03-09"]["respiratory_rate"] == 15.0


class TestParseAllMetrics:
    def _metrics_dir(self, fixtures_dir: Path, tmp_path: Path) -> Path:
        """Copy only the metrics JSON files into a temp directory."""
        import shutil

        metrics_dir = tmp_path / "Metrics"
        metrics_dir.mkdir()
        for name in ("activity.json", "heart.json", "mobility.json"):
            shutil.copy(fixtures_dir / name, metrics_dir / name)
        return metrics_dir

    def test_merges_multiple_files(self, fixtures_dir: Path, tmp_path: Path) -> None:
        result = parse_all_metrics(self._metrics_dir(fixtures_dir, tmp_path))
        day = result["2026-03-09"]
        # From activity.json
        assert "steps" in day
        # From heart.json
        assert "resting_hr" in day
        # From mobility.json
        assert "walking_speed_kmh" in day

    def test_all_dates_present(self, fixtures_dir: Path, tmp_path: Path) -> None:
        result = parse_all_metrics(self._metrics_dir(fixtures_dir, tmp_path))
        assert "2026-03-09" in result
        assert "2026-03-10" in result


def _payload(name: str, entries: list[dict], units: str = "count") -> dict:
    """Wrap metric entries in the Auto Export envelope."""
    return {"data": {"metrics": [{"name": name, "units": units, "data": entries}]}}


class TestTimeGroupingIndependence:
    """A day's value must not depend on Auto Export's Time Grouping setting.

    Regression cover for 2026-08-21: the generic qty path assigned rather than
    accumulated, so an hourly-grouped export reduced each day to its *last*
    entry. A day of 9,462 steps was stored as 5, and the import reported
    success. Daily-grouped exports masked it by sending exactly one entry.
    """

    def test_hourly_step_count_sums_to_the_day_total(self) -> None:
        entries = [
            {"date": f"2026-03-02 {hour:02d}:00:00 +0000", "qty": 100.0}
            for hour in range(24)
        ]
        result = parse_metrics_payload(_payload("step_count", entries))
        assert result["2026-03-02"]["steps"] == 2400.0

    def test_daily_step_count_is_unchanged(self) -> None:
        """Summing is identity for a single entry, so Daily grouping still works."""
        result = parse_metrics_payload(
            _payload(
                "step_count", [{"date": "2026-03-02 00:00:00 +0000", "qty": 9462.0}]
            )
        )
        assert result["2026-03-02"]["steps"] == 9462.0

    def test_hourly_and_daily_agree_on_the_same_day(self) -> None:
        hourly = parse_metrics_payload(
            _payload(
                "step_count",
                [
                    {"date": "2026-03-02 07:00:00 +0000", "qty": 4000.0},
                    {"date": "2026-03-02 08:00:00 +0000", "qty": 5462.0},
                ],
            )
        )
        daily = parse_metrics_payload(
            _payload(
                "step_count", [{"date": "2026-03-02 00:00:00 +0000", "qty": 9462.0}]
            )
        )
        assert hourly["2026-03-02"]["steps"] == daily["2026-03-02"]["steps"]

    def test_distance_and_energy_also_sum(self) -> None:
        """Every running total shares the bug, not just steps."""
        for name, field, unit in (
            ("walking_running_distance", "distance_km", "km"),
            ("active_energy", "active_energy_kj", "kJ"),
            ("apple_exercise_time", "exercise_min", "min"),
            ("flights_climbed", "flights_climbed", "count"),
        ):
            result = parse_metrics_payload(
                _payload(
                    name,
                    [
                        {"date": "2026-03-02 07:00:00 +0000", "qty": 2.0},
                        {"date": "2026-03-02 08:00:00 +0000", "qty": 3.0},
                    ],
                    unit,
                )
            )
            assert result["2026-03-02"][field] == 5.0, name

    def test_rates_are_averaged_not_summed(self) -> None:
        """Resting HR is a level; summing hourly entries would invent 3x the value."""
        result = parse_metrics_payload(
            _payload(
                "resting_heart_rate",
                [
                    {"date": "2026-03-02 00:00:00 +0000", "qty": 50.0},
                    {"date": "2026-03-02 01:00:00 +0000", "qty": 52.0},
                    {"date": "2026-03-02 02:00:00 +0000", "qty": 54.0},
                ],
                "count/min",
            )
        )
        assert result["2026-03-02"]["resting_hr"] == 52.0

    def test_heart_rate_extremes_span_the_whole_day(self) -> None:
        """hr_day_max is the day's peak, not the final hour's peak."""
        result = parse_metrics_payload(
            _payload(
                "heart_rate",
                [
                    {"date": "2026-03-02 09:00:00 +0000", "Min": 48, "Max": 165},
                    {"date": "2026-03-02 23:00:00 +0000", "Min": 55, "Max": 70},
                ],
                "count/min",
            )
        )
        day = result["2026-03-02"]
        assert day["hr_day_min"] == 48.0
        assert day["hr_day_max"] == 165.0

    def test_overnight_metrics_stay_averaged_across_the_night(self) -> None:
        """Night attribution must survive the accumulator rewrite."""
        result = parse_metrics_payload(
            _payload(
                "respiratory_rate",
                [
                    {"date": "2026-03-02 23:00:00 +0000", "qty": 15.0},
                    {"date": "2026-03-03 02:00:00 +0000", "qty": 17.0},
                ],
                "count/min",
            )
        )
        assert result["2026-03-02"]["respiratory_rate"] == 16.0
        assert "2026-03-03" not in result


class TestImperialMetrics:
    """A phone exporting miles, feet and degF stores the same metric figures."""

    @pytest.fixture
    def day(self, fixtures_dir: Path) -> dict[str, float]:
        return parse_metrics_file(fixtures_dir / "imperial_metrics.json")["2026-03-09"]

    def test_distance_miles(self, day: dict[str, float]) -> None:
        assert day["distance_km"] == pytest.approx(3.1 * 1.609344)

    def test_energy_kcal(self, day: dict[str, float]) -> None:
        assert day["active_energy_kj"] == pytest.approx(500 * 4.184)

    def test_walking_speed_mi_per_hr(self, day: dict[str, float]) -> None:
        assert day["walking_speed_kmh"] == pytest.approx(3.0 * 1.609344)

    def test_running_speed_mph(self, day: dict[str, float]) -> None:
        assert day["running_speed_kmh"] == pytest.approx(6.0 * 1.609344)

    def test_step_length_inches(self, day: dict[str, float]) -> None:
        assert day["walking_step_length_cm"] == pytest.approx(28 * 2.54)

    def test_stride_length_feet(self, day: dict[str, float]) -> None:
        assert day["running_stride_length_m"] == pytest.approx(3.5 * 0.3048)

    def test_stair_speed_feet_per_second(self, day: dict[str, float]) -> None:
        assert day["stair_speed_up_ms"] == pytest.approx(0.3048)

    def test_wrist_temperature_degf(self, fixtures_dir: Path) -> None:
        result = parse_metrics_file(fixtures_dir / "imperial_metrics.json")
        assert result["2026-03-09"]["sleeping_wrist_temp_c"] == pytest.approx(
            36.5, abs=0.01
        )

    def test_invariant_metric_untouched(self, day: dict[str, float]) -> None:
        assert day["resting_hr"] == 52.0

    def test_unknown_metric_not_stored(self, day: dict[str, float]) -> None:
        assert "totally_unknown_metric" not in day


class TestUnknownUnits:
    def test_unknown_distance_unit_is_dropped_and_warned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = _payload(
            "walking_running_distance",
            [{"date": "2026-03-02 00:00:00 +0000", "qty": 4.0}],
            "furlong",
        )
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            result = parse_metrics_payload(payload)
        assert "distance_km" not in result.get("2026-03-02", {})
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "walking_running_distance" in message
        assert "furlong" in message

    def test_unknown_metric_name_is_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = _payload(
            "totally_unknown_metric",
            [{"date": "2026-03-02 00:00:00 +0000", "qty": 1.0}],
            "??",
        )
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert parse_metrics_payload(payload) == {}
        assert caplog.records == []

    def test_missing_units_key_drops_distance(self) -> None:
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "walking_running_distance",
                        "data": [{"date": "2026-03-02 00:00:00 +0000", "qty": 4.0}],
                    }
                ]
            }
        }
        assert parse_metrics_payload(payload) == {}

    def test_missing_units_key_keeps_invariant_metric(self) -> None:
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "step_count",
                        "data": [{"date": "2026-03-02 00:00:00 +0000", "qty": 4.0}],
                    }
                ]
            }
        }
        assert parse_metrics_payload(payload)["2026-03-02"]["steps"] == 4.0

    def _sleep_payload(self, units: str, total: float) -> dict:
        return _payload(
            "sleep_analysis",
            [
                {
                    "date": "2026-03-10 07:00:00 +0000",
                    "sleepStart": "2026-03-09 23:30:00 +0000",
                    "totalSleep": total,
                    "deep": total / 7,
                    "core": total / 2,
                    "rem": total / 4,
                    "awake": 0.0,
                }
            ],
            units,
        )

    def test_sleep_in_minutes_is_converted_to_hours(self) -> None:
        result = parse_metrics_payload(self._sleep_payload("min", 420.0))
        assert result["2026-03-09"]["sleep_total_h"] == 7.0

    def test_sleep_in_minutes_with_null_total_is_skipped(self) -> None:
        payload = self._sleep_payload("min", 420.0)
        payload["data"]["metrics"][0]["data"][0]["totalSleep"] = None
        assert parse_metrics_payload(payload) == {}

    def test_sleep_in_bogus_unit_is_dropped(self) -> None:
        assert parse_metrics_payload(self._sleep_payload("fortnights", 7.0)) == {}

    def test_heart_rate_in_bpm_is_accepted(self) -> None:
        payload = _payload(
            "heart_rate",
            [{"date": "2026-03-02 09:00:00 +0000", "Min": 48, "Max": 165}],
            "bpm",
        )
        assert parse_metrics_payload(payload)["2026-03-02"]["hr_day_max"] == 165.0

    def test_heart_rate_in_bogus_unit_is_kept(self) -> None:
        payload = _payload(
            "heart_rate",
            [{"date": "2026-03-02 09:00:00 +0000", "Min": 48, "Max": 165}],
            "beats",
        )
        assert parse_metrics_payload(payload)["2026-03-02"]["hr_day_max"] == 165.0

    def test_invariant_metric_with_unknown_unit_is_kept_and_warned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = _payload(
            "vo2_max", [{"date": "2026-03-02 00:00:00 +0000", "qty": 45.2}], "ml/kg·min"
        )
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            result = parse_metrics_payload(payload)
        assert result["2026-03-02"]["vo2max"] == 45.2
        assert "assumed to be" in caplog.records[0].getMessage()
