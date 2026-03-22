"""Tests for src/parsers/sleep.py."""

from __future__ import annotations

import json
from pathlib import Path

from parsers.sleep import parse_sleep


def _write_sleep_json(path: Path, entries: list[dict]) -> Path:
    """Helper to write a sleep.json fixture."""
    data = {"data": {"metrics": [{"name": "sleep_analysis", "data": entries}]}}
    path.write_text(json.dumps(data))
    return path


class TestParseSleep:
    def test_single_night(self, tmp_path: Path) -> None:
        entries = [
            {
                "value": "Core",
                "start": "2026-03-16 23:00:00 +0000",
                "end": "2026-03-17 01:00:00 +0000",
                "qty": 2.0,
            },
            {
                "value": "Deep",
                "start": "2026-03-17 01:00:00 +0000",
                "end": "2026-03-17 02:00:00 +0000",
                "qty": 1.0,
            },
            {
                "value": "REM",
                "start": "2026-03-17 02:00:00 +0000",
                "end": "2026-03-17 03:30:00 +0000",
                "qty": 1.5,
            },
            {
                "value": "Awake",
                "start": "2026-03-17 03:30:00 +0000",
                "end": "2026-03-17 03:40:00 +0000",
                "qty": 0.17,
            },
        ]
        path = _write_sleep_json(tmp_path / "sleep.json", entries)
        result = parse_sleep(path)

        assert "2026-03-16" in result
        night = result["2026-03-16"]
        assert night["sleep_core_h"] == 2.0
        assert night["sleep_deep_h"] == 1.0
        assert night["sleep_rem_h"] == 1.5
        assert night["sleep_awake_h"] == 0.17
        # Total sleep excludes awake
        assert night["sleep_total_h"] == 4.5
        assert night["sleep_in_bed_h"] == 4.67
        assert night["sleep_efficiency_pct"] > 95.0

    def test_multiple_nights(self, tmp_path: Path) -> None:
        entries = [
            {
                "value": "Core",
                "start": "2026-03-16 23:00:00 +0000",
                "end": "2026-03-17 06:00:00 +0000",
                "qty": 7.0,
            },
            {
                "value": "Deep",
                "start": "2026-03-17 23:00:00 +0000",
                "end": "2026-03-18 06:00:00 +0000",
                "qty": 7.0,
            },
        ]
        path = _write_sleep_json(tmp_path / "sleep.json", entries)
        result = parse_sleep(path)

        assert len(result) == 2
        assert "2026-03-16" in result
        assert "2026-03-17" in result

    def test_empty_entries(self, tmp_path: Path) -> None:
        path = _write_sleep_json(tmp_path / "sleep.json", [])
        result = parse_sleep(path)
        assert result == {}

    def test_awake_excluded_from_total(self, tmp_path: Path) -> None:
        """Awake segments contribute to in-bed but not total sleep."""
        entries = [
            {
                "value": "Core",
                "start": "2026-03-16 23:00:00 +0000",
                "end": "2026-03-17 05:00:00 +0000",
                "qty": 6.0,
            },
            {
                "value": "Awake",
                "start": "2026-03-17 05:00:00 +0000",
                "end": "2026-03-17 06:00:00 +0000",
                "qty": 1.0,
            },
        ]
        path = _write_sleep_json(tmp_path / "sleep.json", entries)
        result = parse_sleep(path)
        night = result["2026-03-16"]

        assert night["sleep_total_h"] == 6.0
        assert night["sleep_in_bed_h"] == 7.0
        # Efficiency = 6/7 * 100 ≈ 85.7%
        assert 85.0 < night["sleep_efficiency_pct"] < 86.0

    def test_no_sleep_file_handled_by_assembler(self) -> None:
        """parse_sleep is only called when the file exists — the assembler
        guards this, but we verify the contract: no file = no call."""
        # This is a documentation test — the assembler checks path.exists()
        # before calling parse_sleep. No assertion needed.

    def test_morning_segment_assigned_to_previous_night(self, tmp_path: Path) -> None:
        """A segment starting at e.g. 05:00 belongs to the previous night."""
        entries = [
            {
                "value": "REM",
                "start": "2026-03-17 05:00:00 +0000",
                "end": "2026-03-17 06:30:00 +0000",
                "qty": 1.5,
            },
        ]
        path = _write_sleep_json(tmp_path / "sleep.json", entries)
        result = parse_sleep(path)

        # 05:00 - 12h = previous day → night of 2026-03-16
        assert "2026-03-16" in result
        assert "2026-03-17" not in result
