"""Tests for src/parsers/gpx.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from parsers.gpx import (
    GPXStats,
    _haversine_m,
    _percentile,
    _rolling_min,
    match_gpx_to_workout,
    parse_gpx_file,
)


class TestHaversine:
    def test_known_distance(self) -> None:
        # Warsaw center to a point ~370m north
        dist = _haversine_m(52.2297, 21.0122, 52.2330, 21.0122)
        assert 360 < dist < 380  # ~367 m

    def test_same_point_is_zero(self) -> None:
        assert _haversine_m(52.2297, 21.0122, 52.2297, 21.0122) == 0.0


class TestRollingMin:
    def test_basic(self) -> None:
        result = _rolling_min([5, 2, 8, 1, 6])
        assert result == [2, 2, 1, 1, 1]

    def test_single_element(self) -> None:
        assert _rolling_min([42]) == [42]

    def test_monotonic(self) -> None:
        result = _rolling_min([1, 2, 3, 4, 5])
        assert result == [1, 1, 2, 3, 4]  # window=3 centered


class TestPercentile:
    def test_median(self) -> None:
        assert _percentile([1, 2, 3, 4, 5], 50) == 3.0

    def test_empty_list(self) -> None:
        assert _percentile([], 50) == 0.0

    def test_p95(self) -> None:
        vals = list(range(1, 101))  # 1..100
        result = _percentile(vals, 95)
        assert 94 < result < 96


class TestParseGpxFile:
    def test_parses_fixture(self, fixtures_dir: Path) -> None:
        stats = parse_gpx_file(fixtures_dir / "route.xml")
        assert stats.distance_km > 0
        assert stats.elevation_gain_m >= 0
        assert stats.avg_speed_ms > 0
        assert stats.max_speed_p95_ms > 0
        assert stats.duration_s == 40.0  # 5 points, 10s apart
        assert stats.start_utc == "2026-03-10T07:00:00Z"

    def test_no_trackpoints_raises(self, tmp_path: Path) -> None:
        gpx = tmp_path / "empty.xml"
        gpx.write_text(
            '<?xml version="1.0"?>'
            '<gpx xmlns="http://www.topografix.com/GPX/1/1">'
            "<trk><trkseg></trkseg></trk></gpx>"
        )
        with pytest.raises(ValueError, match="No trackpoints"):
            parse_gpx_file(gpx)


class TestMatchGpxToWorkout:
    def test_exact_match(self) -> None:
        stats = GPXStats(
            distance_km=5.0,
            elevation_gain_m=40.0,
            avg_speed_ms=3.0,
            max_speed_p95_ms=3.5,
            duration_s=1800,
            start_utc="2026-03-10T07:00:00Z",
            bbox={"lat_min": 52.0, "lat_max": 52.1, "lon_min": 21.0, "lon_max": 21.1},
        )
        index = {"2026-03-10T07:00:00Z": stats}
        result = match_gpx_to_workout("2026-03-10T07:00:00Z", index)
        assert result is stats

    def test_within_tolerance(self) -> None:
        stats = GPXStats(
            distance_km=5.0,
            elevation_gain_m=40.0,
            avg_speed_ms=3.0,
            max_speed_p95_ms=3.5,
            duration_s=1800,
            start_utc="2026-03-10T07:00:30Z",
            bbox={"lat_min": 52.0, "lat_max": 52.1, "lon_min": 21.0, "lon_max": 21.1},
        )
        index = {"2026-03-10T07:00:30Z": stats}
        result = match_gpx_to_workout("2026-03-10T07:00:00Z", index)
        assert result is stats  # 30s < 60s tolerance

    def test_no_match_beyond_tolerance(self) -> None:
        stats = GPXStats(
            distance_km=5.0,
            elevation_gain_m=40.0,
            avg_speed_ms=3.0,
            max_speed_p95_ms=3.5,
            duration_s=1800,
            start_utc="2026-03-10T08:00:00Z",
            bbox={"lat_min": 52.0, "lat_max": 52.1, "lon_min": 21.0, "lon_max": 21.1},
        )
        index = {"2026-03-10T08:00:00Z": stats}
        result = match_gpx_to_workout("2026-03-10T07:00:00Z", index)
        assert result is None

    def test_malformed_workout_time(self) -> None:
        result = match_gpx_to_workout("not-a-date", {})
        assert result is None
