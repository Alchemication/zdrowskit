"""Parse workout JSON files.

Supports two export formats:
  - Shortcuts: single workouts.json with nested qty/units dicts.
  - Auto Export: N files by time period (HealthAutoExport-YYYY-WW.json), same
    workout schema but with embedded route trackpoints and summary stats.

Schema: {"data": {"workouts": [...]}}

Public API:
    parse_workouts(path)          -- parse a single workouts JSON file
    parse_workouts_dir(directory) -- parse all JSON files in a directory, deduplicated

Example:
    from pathlib import Path
    from parsers.workouts import parse_workouts, parse_workouts_dir

    workouts = parse_workouts(Path("Workouts/workouts.json"))
    workouts = parse_workouts_dir(Path("Workouts/"))
"""

from __future__ import annotations
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import WORKOUT_SPLIT_MIN_SAMPLE_COVERAGE
from models import WorkoutSnapshot, WorkoutSplit


# Maps workout name → category. Auto Export writes Apple's own activity names,
# which are gerunds for some types ("Outdoor Cycling") and not others ("Outdoor
# Run"); the older Shortcuts format wrote the shorter "Outdoor Cycle". Both
# spellings are listed because a name that misses lands in "other", where it is
# silently excluded from every category-filtered query rather than erroring.
_CATEGORY_MAP: dict[str, str] = {
    "outdoor run": "run",
    "indoor run": "run",
    "run": "run",
    "treadmill running": "run",
    "traditional strength training": "lift",
    "functional strength training": "lift",
    "outdoor walk": "walk",
    "indoor walk": "walk",
    # Hiking is outdoor ambulatory activity with usable route data. Walks feed
    # only walk_count and no baseline, so folding hikes in adds them to volume
    # and earns them splits without skewing a pace norm.
    "hiking": "walk",
    "outdoor cycle": "cycle",
    "indoor cycle": "cycle",
    "outdoor cycling": "cycle",
    "indoor cycling": "cycle",
}

_MIN_WORKOUT_DURATION_MIN = 1.0
_FUNCTIONAL_LIFT_MIN_DURATION = 15.0
_SPLIT_DISTANCE_M = 1000.0
_SPLIT_DISTANCE_EPSILON_M = 1e-3

# Max plausible per-segment speed (m/s) before treating a trackpoint pair as a
# GPS glitch. Running world-record 1500 m pace is ~7.3 m/s; 10 m/s covers every
# real human run split. Cycling leaves headroom for descents. Walks are capped
# above a run-shaped burst rather than at walking pace, since an "Outdoor Walk"
# routinely contains jogged crossings and hikes contain scrambles.
_MAX_SEGMENT_SPEED_MS: dict[str, float] = {"run": 10.0, "cycle": 25.0, "walk": 6.0}

# Max plausible per-segment distance (m). Apple samples GPS at 1–5 s during
# activity, so even fast cycling yields sub-100 m segments. A single segment
# above this cap indicates a GPS dropout where sampling resumed far away; its
# proportional time split produces phantom sub-elite paces. Caps are generous
# to stay well above any legitimate observed segment.
_MAX_SEGMENT_DISTANCE_M: dict[str, float] = {
    "run": 1200.0,
    "cycle": 3000.0,
    "walk": 1200.0,
}

# Nominal span of one Apple per-minute workout bin (heart rate, step count).
# The export stamps each bin at its start with no end time, spaced exactly 60 s
# apart in observed data. A bin is treated as covering at most this long so a
# sampling dropout reads as an uncovered gap rather than one reading smeared
# across the minutes it was absent for.
_BIN_SECONDS = 60.0


def _category(name: str) -> str:
    """Map a workout name to its normalised category string.

    Args:
        name: Raw workout name from the JSON, e.g. "Outdoor Run".

    Returns:
        One of "run", "lift", "walk", "cycle", or "other".
    """
    return _CATEGORY_MAP.get(name.lower(), "other")


def _counts_as_lift(name: str, duration_min: float) -> bool:
    """Return whether a workout should count as a completed lift.

    Args:
        name: Raw workout name from the JSON.
        duration_min: Elapsed workout duration in minutes.

    Returns:
        True when the workout should count toward weekly lift completion.
    """
    normalized = name.lower()
    if normalized == "traditional strength training":
        return True
    if normalized == "functional strength training":
        return duration_min >= _FUNCTIONAL_LIFT_MIN_DURATION
    return False


def _qty(obj: dict | None) -> float | None:
    """Safely extract the numeric value from an Apple Health qty dict.

    Args:
        obj: A dict like ``{"qty": 81.6, "units": "count/min"}``, or None.

    Returns:
        The float value of "qty", or None if obj is None or "qty" is absent.
    """
    if obj is None:
        return None
    v = obj.get("qty")
    return float(v) if v is not None else None


def _parse_apple_dt(raw: str) -> datetime:
    """Parse an Apple Health datetime string to a UTC datetime.

    Args:
        raw: String in the form '2026-03-14 06:47:51 +0000'.

    Returns:
        A timezone-aware datetime normalised to UTC.
    """
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)


def _duration_min(w: dict, start_dt: datetime) -> float:
    """Return workout duration in minutes from explicit or derived fields.

    Args:
        w: Raw workout dict from the JSON.
        start_dt: Parsed workout start datetime in UTC.

    Returns:
        Workout duration in minutes.
    """
    duration_s = w.get("duration")
    if duration_s is not None:
        return float(duration_s) / 60.0

    end_raw = w.get("end")
    if isinstance(end_raw, str):
        end_dt = _parse_apple_dt(end_raw)
        return max(0.0, (end_dt - start_dt).total_seconds() / 60.0)

    return 0.0


def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile using linear interpolation.

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


def _extract_route_stats(w: dict) -> dict[str, float | None]:
    """Extract route/distance stats from Auto Export workout fields.

    Uses Apple-computed summary fields (distance, speed, elevationUp) when
    available, and computes p95 max speed from embedded route trackpoints.

    Args:
        w: Raw workout dict from the JSON.

    Returns:
        Dict with gpx_distance_km, gpx_elevation_gain_m, gpx_avg_speed_ms,
        gpx_max_speed_p95_ms — any may be None if data is absent.
    """
    distance_km = _qty(w.get("distance"))
    elevation_m = _qty(w.get("elevationUp"))

    # Convert speed from km/h to m/s
    speed_kmh = _qty(w.get("speed"))
    avg_speed_ms = round(speed_kmh / 3.6, 4) if speed_kmh is not None else None

    # Compute p95 max speed from route trackpoints
    max_speed_p95_ms: float | None = None
    route = w.get("route", [])
    if route:
        speeds = [pt["speed"] for pt in route if pt.get("speed", 0) > 0]
        if speeds:
            max_speed_p95_ms = round(_percentile(speeds, 95), 4)

    return {
        "gpx_distance_km": round(distance_km, 3) if distance_km is not None else None,
        "gpx_elevation_gain_m": elevation_m,
        "gpx_avg_speed_ms": avg_speed_ms,
        "gpx_max_speed_p95_ms": max_speed_p95_ms,
    }


def _extract_start_location(w: dict) -> tuple[float, float] | None:
    """Return the first valid route coordinate for locality lookup."""
    route = w.get("route", [])
    if not isinstance(route, list):
        return None
    for point in route:
        if not isinstance(point, dict):
            continue
        lat = _finite_float(point.get("latitude"))
        lon = _finite_float(point.get("longitude"))
        if lat is not None and lon is not None:
            return lat, lon
    return None


def _finite_float(value: object) -> float | None:
    """Return a finite float or None for invalid/missing values."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_route_timestamp(raw: object) -> datetime | None:
    """Parse a route-point timestamp into UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None

    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_bins(
    w: dict,
    key: str,
    value_field: str,
    max_field: str | None = None,
) -> list[tuple[datetime, datetime, float, float | None]]:
    """Build sorted, non-overlapping one-minute bins from a workout series.

    Auto Export ships several per-minute series in the same shape — heart rate
    as ``heartRateData`` with {Min, Avg, Max}, step count as ``stepCount`` with
    ``qty``. Each entry carries a start ``date`` but no end time, so a bin is
    closed at the earlier of the next bin's start and ``_BIN_SECONDS`` after its
    own: consecutive bins tile the workout, while a sampling dropout leaves a
    real hole instead of stretching one reading over it.

    Args:
        w: Raw workout dict from the JSON.
        key: Workout field holding the series, e.g. "heartRateData".
        value_field: Entry field holding the primary value, e.g. "Avg".
        max_field: Entry field holding a per-bin maximum, if the series has one.

    Returns:
        A list of (start, end, value, max_value) tuples ordered by start. Entries
        without a usable timestamp or a positive value are dropped; max_value is
        None when the series has no maximum or the export omits it.
    """
    raw = w.get(key)
    if not isinstance(raw, list):
        return []

    parsed: list[tuple[datetime, float, float | None]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        start = _parse_route_timestamp(entry.get("date"))
        value = _finite_float(entry.get(value_field))
        if start is None or value is None or value <= 0:
            continue
        max_value = _finite_float(entry.get(max_field)) if max_field else None
        parsed.append((start, value, max_value))

    parsed.sort(key=lambda item: item[0])

    bins: list[tuple[datetime, datetime, float, float | None]] = []
    for index, (start, value, max_value) in enumerate(parsed):
        end = start + timedelta(seconds=_BIN_SECONDS)
        if index + 1 < len(parsed):
            end = min(end, parsed[index + 1][0])
        if end > start:
            bins.append((start, end, value, max_value))
    return bins


def _hr_for_window(
    bins: list[tuple[datetime, datetime, float, float | None]],
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[float | None, int | None, float | None]:
    """Summarise heart rate over one split's wall-clock window.

    The average is weighted by how long each bin overlaps the window, so a bin
    straddling a kilometre boundary contributes to both splits in proportion.

    Args:
        bins: Heart-rate bins from ``_extract_bins``.
        window_start: Wall-clock start of the split.
        window_end: Wall-clock end of the split.

    Returns:
        A (hr_avg, hr_max, coverage) tuple. Coverage is the fraction of the
        window backed by samples, reported whenever the window is valid. hr_avg
        and hr_max are None when coverage is below
        ``WORKOUT_SPLIT_MIN_SAMPLE_COVERAGE``, so a thinly sampled kilometre
        reads as unknown rather than as a confident number drawn from part of it.
    """
    if window_start is None or window_end is None:
        return None, None, None

    window_s = (window_end - window_start).total_seconds()
    if window_s <= 0:
        return None, None, None

    weighted_sum = 0.0
    covered_s = 0.0
    peak_bpm: float | None = None
    for bin_start, bin_end, avg_bpm, max_bpm in bins:
        overlap_s = (
            min(window_end, bin_end) - max(window_start, bin_start)
        ).total_seconds()
        if overlap_s <= 0:
            continue
        weighted_sum += avg_bpm * overlap_s
        covered_s += overlap_s
        candidate = max_bpm if max_bpm is not None else avg_bpm
        if peak_bpm is None or candidate > peak_bpm:
            peak_bpm = candidate

    coverage = round(min(1.0, covered_s / window_s), 4)
    if covered_s <= 0 or coverage < WORKOUT_SPLIT_MIN_SAMPLE_COVERAGE:
        return None, None, coverage

    return (
        round(weighted_sum / covered_s, 1),
        int(round(peak_bpm)) if peak_bpm is not None else None,
        coverage,
    )


def _cadence_for_window(
    bins: list[tuple[datetime, datetime, float, float | None]],
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[float | None, float | None]:
    """Summarise step cadence over one split's wall-clock window.

    Unlike heart rate, a step-count bin holds a total rather than a rate, so an
    overlapping bin contributes the share of its steps matching the share of its
    span inside the window. Cadence is then those steps over the time actually
    sampled, which keeps it a true rate even when part of the split is missing —
    the coverage floor decides separately whether that rate may stand in for the
    whole kilometre.

    Args:
        bins: Step-count bins from ``_extract_bins``.
        window_start: Wall-clock start of the split.
        window_end: Wall-clock end of the split.

    Returns:
        A (cadence_spm, coverage) tuple in steps per minute. cadence_spm is None
        when coverage is below ``WORKOUT_SPLIT_MIN_SAMPLE_COVERAGE``.
    """
    if window_start is None or window_end is None:
        return None, None

    window_s = (window_end - window_start).total_seconds()
    if window_s <= 0:
        return None, None

    steps = 0.0
    covered_s = 0.0
    for bin_start, bin_end, bin_steps, _ in bins:
        overlap_s = (
            min(window_end, bin_end) - max(window_start, bin_start)
        ).total_seconds()
        if overlap_s <= 0:
            continue
        bin_span_s = (bin_end - bin_start).total_seconds()
        steps += bin_steps * (overlap_s / bin_span_s)
        covered_s += overlap_s

    coverage = round(min(1.0, covered_s / window_s), 4)
    if covered_s <= 0 or coverage < WORKOUT_SPLIT_MIN_SAMPLE_COVERAGE:
        return None, coverage

    return round(steps / (covered_s / 60.0), 1), coverage


def _haversine_m(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Return the great-circle distance in metres between two points."""
    radius_m = 6_371_000.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_m * c


def _extract_splits(
    w: dict,
    category: str,
    session_elevation_gain_m: float | None,
) -> list[WorkoutSplit]:
    """Derive 1 km splits from embedded route trackpoints.

    Splits are based on haversine distance between consecutive points. Pace is
    elapsed wall-clock time between kilometre boundaries. Average speed is a
    time-weighted representative speed per segment, preferring trackpoint
    speed values when present and falling back to distance / elapsed time.

    Segments implying an implausible speed for the category (GPS teleports) are
    skipped so a single bad trackpoint cannot produce a phantom PR split. Only
    ``run``, ``walk``, and ``cycle`` categories produce splits — swims, paddles,
    and other activities have unreliable route data.

    Heart rate is attached per split by time-weighting the workout's one-minute
    bins across the split's wall-clock window, with the resulting coverage
    recorded so a partially sampled kilometre is not passed off as a measured
    one.

    When ``session_elevation_gain_m`` is available, per-split elevation gain
    and loss are rescaled so the per-split gains sum to Apple's authoritative
    session total, since raw per-sample altitude deltas are dominated by GPS
    jitter and overcount real climbs by several multiples.

    Args:
        w: Raw workout dict from the JSON.
        category: Normalised workout category string.
        session_elevation_gain_m: Apple-reported session elevation gain, if any.

    Returns:
        A list of complete 1 km splits. Workouts shorter than 1 km, unsupported
        categories, or routes without usable trackpoints return an empty list.
    """
    speed_cap_ms = _MAX_SEGMENT_SPEED_MS.get(category)
    distance_cap_m = _MAX_SEGMENT_DISTANCE_M.get(category)
    if speed_cap_ms is None or distance_cap_m is None:
        return []

    route = w.get("route", [])
    if not isinstance(route, list) or len(route) < 2:
        return []

    hr_bins = _extract_bins(w, "heartRateData", "Avg", max_field="Max")
    step_bins = _extract_bins(w, "stepCount", "qty")

    km_index = 1
    split_distance_m = 0.0
    split_elapsed_s = 0.0
    split_speed_weighted = 0.0
    split_speed_time_s = 0.0
    split_elevation_gain_m = 0.0
    split_elevation_loss_m = 0.0
    split_has_elevation = False
    # Wall-clock cursor tracking the position reached inside the route. Splits
    # fall part-way through a segment, so boundary times are interpolated rather
    # than taken from a trackpoint.
    split_start_ts: datetime | None = None
    cursor_ts: datetime | None = None
    splits: list[WorkoutSplit] = []

    def add_piece(
        distance_m: float,
        elapsed_s: float,
        segment_speed_ms: float | None,
        elevation_delta_m: float | None,
        fraction: float,
    ) -> None:
        """Accumulate a segment fraction into the current split."""
        nonlocal split_distance_m
        nonlocal split_elapsed_s
        nonlocal split_speed_weighted
        nonlocal split_speed_time_s
        nonlocal split_elevation_gain_m
        nonlocal split_elevation_loss_m
        nonlocal split_has_elevation
        nonlocal cursor_ts

        piece_distance_m = distance_m * fraction
        piece_elapsed_s = elapsed_s * fraction
        split_distance_m += piece_distance_m
        split_elapsed_s += piece_elapsed_s

        if cursor_ts is not None:
            cursor_ts += timedelta(seconds=piece_elapsed_s)

        if segment_speed_ms is not None and piece_elapsed_s > 0:
            split_speed_weighted += segment_speed_ms * piece_elapsed_s
            split_speed_time_s += piece_elapsed_s

        if elevation_delta_m is not None:
            piece_elevation_m = elevation_delta_m * fraction
            split_has_elevation = True
            if piece_elevation_m >= 0:
                split_elevation_gain_m += piece_elevation_m
            else:
                split_elevation_loss_m += abs(piece_elevation_m)

    def flush_split() -> None:
        """Emit the current split and reset accumulators for the next km."""
        nonlocal km_index
        nonlocal split_distance_m
        nonlocal split_elapsed_s
        nonlocal split_speed_weighted
        nonlocal split_speed_time_s
        nonlocal split_elevation_gain_m
        nonlocal split_elevation_loss_m
        nonlocal split_has_elevation
        nonlocal split_start_ts

        if split_elapsed_s <= 0:
            return

        avg_speed_ms: float | None = None
        if split_speed_time_s > 0:
            avg_speed_ms = round(split_speed_weighted / split_speed_time_s, 4)

        elevation_gain_m: float | None = None
        elevation_loss_m: float | None = None
        if split_has_elevation:
            elevation_gain_m = round(split_elevation_gain_m, 2)
            elevation_loss_m = round(split_elevation_loss_m, 2)

        hr_avg, hr_max, hr_coverage = _hr_for_window(hr_bins, split_start_ts, cursor_ts)
        cadence_spm, cadence_coverage = _cadence_for_window(
            step_bins, split_start_ts, cursor_ts
        )

        splits.append(
            WorkoutSplit(
                km_index=km_index,
                pace_min_km=round(split_elapsed_s / 60.0, 4),
                avg_speed_ms=avg_speed_ms,
                elevation_gain_m=elevation_gain_m,
                elevation_loss_m=elevation_loss_m,
                hr_avg=hr_avg,
                hr_max=hr_max,
                hr_coverage=hr_coverage,
                cadence_spm=cadence_spm,
                cadence_coverage=cadence_coverage,
            )
        )

        split_start_ts = cursor_ts

        km_index += 1
        split_distance_m = 0.0
        split_elapsed_s = 0.0
        split_speed_weighted = 0.0
        split_speed_time_s = 0.0
        split_elevation_gain_m = 0.0
        split_elevation_loss_m = 0.0
        split_has_elevation = False

    for prev_point, point in zip(route, route[1:]):
        prev_lat = _finite_float(prev_point.get("latitude"))
        prev_lon = _finite_float(prev_point.get("longitude"))
        lat = _finite_float(point.get("latitude"))
        lon = _finite_float(point.get("longitude"))
        if None in (prev_lat, prev_lon, lat, lon):
            continue

        prev_ts = _parse_route_timestamp(prev_point.get("timestamp"))
        ts = _parse_route_timestamp(point.get("timestamp"))
        if prev_ts is None or ts is None:
            continue

        elapsed_s = max(0.0, (ts - prev_ts).total_seconds())
        distance_m = _haversine_m(prev_lat, prev_lon, lat, lon)

        # Drop GPS-teleport segments (single-point warps) and multi-km dropout
        # segments that would otherwise populate phantom sub-elite splits.
        # Zero-elapsed segments are also skipped since they imply infinite
        # speed.
        if (
            elapsed_s <= 0
            or distance_m > distance_cap_m
            or distance_m / elapsed_s > speed_cap_ms
        ):
            continue

        # Re-anchor the cursor to this segment's real start time. Skipped
        # glitch segments never advance it, so interpolating forward without a
        # resync would drift; the first accepted segment also opens split 1.
        if cursor_ts is None:
            split_start_ts = prev_ts
        cursor_ts = prev_ts

        prev_altitude = _finite_float(prev_point.get("altitude"))
        altitude = _finite_float(point.get("altitude"))
        elevation_delta_m: float | None = None
        if prev_altitude is not None and altitude is not None:
            elevation_delta_m = altitude - prev_altitude

        prev_speed = _finite_float(prev_point.get("speed"))
        speed = _finite_float(point.get("speed"))
        segment_speed_ms: float | None
        if prev_speed is not None and speed is not None:
            segment_speed_ms = (prev_speed + speed) / 2.0
        elif speed is not None:
            segment_speed_ms = speed
        elif prev_speed is not None:
            segment_speed_ms = prev_speed
        elif elapsed_s > 0:
            segment_speed_ms = distance_m / elapsed_s
        else:
            segment_speed_ms = None

        remaining_distance_m = distance_m
        remaining_elapsed_s = elapsed_s
        remaining_elevation_delta_m = elevation_delta_m

        while (
            remaining_distance_m > 0
            and split_distance_m + remaining_distance_m
            >= _SPLIT_DISTANCE_M - _SPLIT_DISTANCE_EPSILON_M
        ):
            distance_needed_m = max(0.0, _SPLIT_DISTANCE_M - split_distance_m)
            fraction = min(1.0, distance_needed_m / remaining_distance_m)
            add_piece(
                remaining_distance_m,
                remaining_elapsed_s,
                segment_speed_ms,
                remaining_elevation_delta_m,
                fraction,
            )
            flush_split()
            remaining_elapsed_s *= 1 - fraction
            if remaining_elevation_delta_m is not None:
                remaining_elevation_delta_m *= 1 - fraction
            remaining_distance_m = max(0.0, remaining_distance_m - distance_needed_m)

        if remaining_distance_m > 0 or remaining_elapsed_s > 0:
            add_piece(
                remaining_distance_m,
                remaining_elapsed_s,
                segment_speed_ms,
                remaining_elevation_delta_m,
                1.0,
            )

    # Per-sample altitude deltas are dominated by GPS jitter and overcount
    # climbs multiple times over. If Apple reports an authoritative session
    # gain, rescale per-split gain and loss by the same factor so the split
    # totals agree with the session-level number.
    if session_elevation_gain_m is not None and splits:
        raw_total_gain = sum(s.elevation_gain_m or 0.0 for s in splits)
        if raw_total_gain > 0:
            scale = session_elevation_gain_m / raw_total_gain
            for split in splits:
                if split.elevation_gain_m is not None:
                    split.elevation_gain_m = round(split.elevation_gain_m * scale, 2)
                if split.elevation_loss_m is not None:
                    split.elevation_loss_m = round(split.elevation_loss_m * scale, 2)

    return splits


def parse_workouts(path: Path) -> list[WorkoutSnapshot]:
    """Parse a workouts JSON file into a list of WorkoutSnapshots.

    Handles both Shortcuts format (single workouts.json) and Auto Export format
    (with embedded route data and summary stats).

    Args:
        path: Path to a workouts JSON file.

    Returns:
        A list of WorkoutSnapshot objects ordered chronologically by start_utc.
    """
    with path.open() as f:
        return parse_workouts_payload(json.load(f))


def parse_workouts_payload(data: dict) -> list[WorkoutSnapshot]:
    """Parse an already-decoded workouts payload.

    Args:
        data: Decoded Auto Export workouts JSON.

    Returns:
        A list of WorkoutSnapshot objects ordered chronologically by start_utc.
    """
    snapshots: list[WorkoutSnapshot] = []

    for w in data["data"]["workouts"]:
        name = w.get("name", "Unknown")
        start_dt = _parse_apple_dt(w["start"])
        duration_min = _duration_min(w, start_dt)

        if duration_min < _MIN_WORKOUT_DURATION_MIN:
            continue

        # Heart rate — nested {"avg": {"qty": x}, "min": {...}, "max": {...}}
        hr_block = w.get("heartRate", {})
        hr_avg = _qty(hr_block.get("avg"))
        hr_min_val = _qty(hr_block.get("min"))
        hr_max_val = _qty(hr_block.get("max"))

        # Fallback: avgHeartRate / maxHeartRate top-level fields
        if hr_avg is None:
            hr_avg = _qty(w.get("avgHeartRate"))
        if hr_max_val is None:
            hr_max_val = _qty(w.get("maxHeartRate"))

        active_energy = _qty(w.get("activeEnergyBurned")) or 0.0
        intensity = _qty(w.get("intensity"))
        temperature = _qty(w.get("temperature"))
        humidity = (
            w.get("humidity", {}).get("qty")
            if isinstance(w.get("humidity"), dict)
            else None
        )

        # Extract embedded route/summary stats (Auto Export format)
        route_stats = _extract_route_stats(w)
        category = _category(name)
        splits = _extract_splits(w, category, route_stats["gpx_elevation_gain_m"])
        start_location = _extract_start_location(w)

        snapshots.append(
            WorkoutSnapshot(
                type=name,
                category=category,
                counts_as_lift=_counts_as_lift(name, duration_min),
                start_utc=start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_min=duration_min,
                hr_min=int(hr_min_val) if hr_min_val is not None else None,
                hr_avg=hr_avg,
                hr_max=int(hr_max_val) if hr_max_val is not None else None,
                active_energy_kj=active_energy,
                intensity_kcal_per_hr_kg=intensity,
                temperature_c=temperature,
                humidity_pct=int(humidity) if humidity is not None else None,
                gpx_distance_km=route_stats["gpx_distance_km"],
                gpx_elevation_gain_m=route_stats["gpx_elevation_gain_m"],
                gpx_avg_speed_ms=route_stats["gpx_avg_speed_ms"],
                gpx_max_speed_p95_ms=route_stats["gpx_max_speed_p95_ms"],
                location_lat=start_location[0] if start_location else None,
                location_lon=start_location[1] if start_location else None,
                splits=splits,
            )
        )

    snapshots.sort(key=lambda s: s.start_utc)
    return snapshots


def parse_workouts_dir(workouts_dir: Path) -> list[WorkoutSnapshot]:
    """Parse all JSON files in a workouts directory and deduplicate.

    Used for Auto Export format where workouts are split across multiple
    time-period files.

    Args:
        workouts_dir: Path to a directory containing workout JSON files.

    Returns:
        A deduplicated list of WorkoutSnapshot objects sorted by start_utc.
    """
    seen: set[str] = set()
    snapshots: list[WorkoutSnapshot] = []

    for json_file in sorted(workouts_dir.glob("*.json")):
        for w in parse_workouts(json_file):
            if w.start_utc not in seen:
                seen.add(w.start_utc)
                snapshots.append(w)

    snapshots.sort(key=lambda s: s.start_utc)
    return snapshots
