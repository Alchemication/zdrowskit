"""Parse MyHealth/Routes/*.xml (GPX 1.1) files and derive per-route stats.

Each file contains one <trk> with a single <trkseg> of <trkpt> elements (~1/sec).
Each <trkpt> has: lat, lon, <ele>, <time>, <extensions> with speed (m/s), course, hAcc, vAcc.

Public API:
    GPXStats                          -- dataclass: distance, elevation, speed stats for one route
    parse_gpx_file(path)              -- parse a single GPX file → GPXStats
    parse_all_gpx(routes_dir)         -- parse all GPX files in Routes/ → dict keyed by start_utc
    match_gpx_to_workout(start, index) -- find the GPXStats closest to a workout start time

Example:
    from pathlib import Path
    from parsers.gpx import parse_all_gpx, match_gpx_to_workout

    index = parse_all_gpx(Path("MyHealth/Routes/"))
    stats = match_gpx_to_workout("2026-03-10T17:04:05Z", index)
    if stats:
        print(stats.distance_km, stats.elevation_gain_m)
"""

from __future__ import annotations
import logging
import math
import statistics
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from re import search as re_search

logger = logging.getLogger(__name__)


GPX_NS = "http://www.topografix.com/GPX/1/1"
_TAG = lambda tag: f"{{{GPX_NS}}}{tag}"  # noqa: E731

# Tolerance in seconds for matching a GPX file's start time to a workout start time
MATCH_TOLERANCE_S = 60


@dataclass
class GPXStats:
    """Derived statistics for a single GPX route file.

    Attributes:
        distance_km: Total haversine distance of the route in kilometres.
        elevation_gain_m: Cumulative positive elevation gain in metres,
            computed after applying a 3-point rolling-minimum to suppress GPS noise.
        avg_speed_ms: Mean speed in m/s taken from the GPX speed extension field.
        max_speed_p95_ms: 95th-percentile speed in m/s (filters GPS spikes).
        duration_s: Elapsed time from first to last trackpoint in seconds.
        start_utc: ISO 8601 UTC timestamp of the first trackpoint.
        bbox: Bounding box dict with keys lat_min, lat_max, lon_min, lon_max.
    """

    distance_km: float
    elevation_gain_m: float
    avg_speed_ms: float
    max_speed_p95_ms: float  # 95th-percentile speed (rejects GPS spikes)
    duration_s: float
    start_utc: str  # ISO datetime of first trackpoint
    bbox: dict[str, float]  # lat_min, lat_max, lon_min, lon_max


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in metres between two lat/lon points.

    Args:
        lat1: Latitude of the first point in decimal degrees.
        lon1: Longitude of the first point in decimal degrees.
        lat2: Latitude of the second point in decimal degrees.
        lon2: Longitude of the second point in decimal degrees.

    Returns:
        Distance in metres.
    """
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def _rolling_min(values: list[float], window: int = 3) -> list[float]:
    """Apply a rolling minimum filter over a list to suppress noise.

    Args:
        values: Input list of float values.
        window: Number of neighbouring elements to include (centred).

    Returns:
        A list of the same length where each element is the minimum within
        the surrounding window.
    """
    half = window // 2
    result = []
    n = len(values)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        result.append(min(values[lo:hi]))
    return result


def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile of a list using linear interpolation.

    Args:
        values: Input list of floats (need not be sorted).
        p: Percentile to compute in the range 0–100.

    Returns:
        The interpolated percentile value, or 0.0 for an empty list.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (p / 100) * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _parse_gpx_time(raw: str) -> datetime:
    """Parse a GPX ISO time string to a UTC datetime.

    Args:
        raw: String in the form '2026-03-09T15:33:14Z'.

    Returns:
        A timezone-aware datetime in UTC.
    """
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_gpx_file(path: Path) -> GPXStats:
    """Parse a single GPX file and return derived route statistics.

    Args:
        path: Path to the GPX XML file.

    Returns:
        A GPXStats dataclass populated with distance, elevation, speed, and
        bounding-box data derived from the trackpoints.

    Raises:
        ValueError: If the file contains no trackpoints.
    """
    delays = (10, 30, 60)
    for attempt in range(len(delays)):
        try:
            tree = ET.parse(path)
            break
        except OSError:
            if attempt < len(delays) - 1:
                logger.debug("Retry %d for %s (iCloud lock)", attempt + 1, path.name)
                time.sleep(delays[attempt])
            else:
                raise
    root = tree.getroot()

    trkpts = root.findall(f".//{_TAG('trkpt')}")
    if not trkpts:
        raise ValueError(f"No trackpoints found in {path}")

    lats, lons, eles, speeds, times = [], [], [], [], []

    for pt in trkpts:
        lats.append(float(pt.get("lat")))
        lons.append(float(pt.get("lon")))

        ele_el = pt.find(_TAG("ele"))
        eles.append(float(ele_el.text) if ele_el is not None else 0.0)

        time_el = pt.find(_TAG("time"))
        if time_el is not None:
            times.append(_parse_gpx_time(time_el.text))

        ext = pt.find(_TAG("extensions"))
        if ext is not None:
            speed_el = ext.find("speed") or ext.find(_TAG("speed"))
            # Try both with and without namespace (Health Auto Export uses no ns on extensions children)
            if speed_el is None:
                # Fallback: iterate children
                for child in ext:
                    if child.tag in ("speed", f"{{{GPX_NS}}}speed"):
                        speed_el = child
                        break
            if speed_el is not None:
                speeds.append(float(speed_el.text))

    # Distance: haversine accumulation
    total_m = sum(
        _haversine_m(lats[i], lons[i], lats[i + 1], lons[i + 1])
        for i in range(len(lats) - 1)
    )

    # Elevation gain: rolling-min smoothed positive deltas
    smoothed_eles = _rolling_min(eles, window=3)
    elevation_gain = sum(
        max(0.0, smoothed_eles[i + 1] - smoothed_eles[i])
        for i in range(len(smoothed_eles) - 1)
    )

    avg_speed = statistics.mean(speeds) if speeds else 0.0
    max_speed_p95 = _percentile(speeds, 95) if speeds else 0.0

    duration_s = (times[-1] - times[0]).total_seconds() if len(times) >= 2 else 0.0
    start_utc = times[0].strftime("%Y-%m-%dT%H:%M:%SZ") if times else ""

    return GPXStats(
        distance_km=total_m / 1000.0,
        elevation_gain_m=round(elevation_gain, 1),
        avg_speed_ms=avg_speed,
        max_speed_p95_ms=max_speed_p95,
        duration_s=duration_s,
        start_utc=start_utc,
        bbox={
            "lat_min": min(lats),
            "lat_max": max(lats),
            "lon_min": min(lons),
            "lon_max": max(lons),
        },
    )


def _extract_filename_dt(path: Path) -> datetime | None:
    """Try to parse the datetime embedded in a route filename.

    Handles names like 'Outdoor Run-Route-2026-03-10 17:04:05.xml'.

    Args:
        path: Path to the GPX file whose stem may contain a datetime.

    Returns:
        A UTC datetime if the pattern is found, otherwise None.
    """
    match = re_search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", path.stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return None


def parse_all_gpx(routes_dir: Path) -> dict[str, GPXStats]:
    """Parse all GPX files in the Routes/ directory.

    The dict key is the route's start_utc (from the first trackpoint), with
    the filename-embedded timestamp as a fallback when the GPX time is absent.
    Files that fail to parse are skipped with a warning printed to stdout.

    Args:
        routes_dir: Path to the directory containing GPX XML files.

    Returns:
        A dict mapping start_utc ISO strings to GPXStats for each parsed file.
    """
    results: dict[str, GPXStats] = {}

    for xml_file in sorted(routes_dir.glob("*.xml")):
        try:
            stats = parse_gpx_file(xml_file)
            key = stats.start_utc

            # If GPX start time is empty, fall back to filename
            if not key:
                fn_dt = _extract_filename_dt(xml_file)
                key = fn_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if fn_dt else str(xml_file)

            results[key] = stats
        except Exception as e:
            logger.warning("could not parse %s: %s", xml_file.name, e)

    return results


def match_gpx_to_workout(
    workout_start_utc: str,
    gpx_index: dict[str, GPXStats],
) -> GPXStats | None:
    """Find the GPXStats whose start time best matches a workout start time.

    Searches gpx_index for an entry whose start_utc is within
    MATCH_TOLERANCE_S (60 s) of workout_start_utc.

    Args:
        workout_start_utc: Workout start time as an ISO 8601 UTC string,
            e.g. "2026-03-10T17:04:05Z".
        gpx_index: Dict of start_utc → GPXStats as returned by parse_all_gpx.

    Returns:
        The matching GPXStats, or None if no route is within the tolerance.
    """
    try:
        wdt = datetime.strptime(workout_start_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None

    for key, stats in gpx_index.items():
        try:
            gdt = datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if abs((wdt - gdt).total_seconds()) <= MATCH_TOLERANCE_S:
            return stats

    return None
