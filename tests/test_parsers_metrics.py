"""Tests for src/parsers/metrics.py."""

from __future__ import annotations

from pathlib import Path

from parsers.metrics import _parse_date, parse_all_metrics, parse_metrics_file


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
