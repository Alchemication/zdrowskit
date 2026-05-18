"""Locality resolution for route-bearing workouts."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from config import (
    LOCATION_COORD_DECIMALS,
    LOCATION_GEOCODER,
    LOCATION_HTTP_TIMEOUT_S,
    LOCATION_MIN_REQUEST_INTERVAL_S,
    LOCATION_USER_AGENT,
)

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_LOCALITY_KEYS = (
    "city",
    "town",
    "village",
    "municipality",
    "hamlet",
    "suburb",
    "city_district",
)
_REGION_KEYS = ("state", "county", "region", "state_district")
_last_request_at = 0.0


def _rounded_coord(lat: float, lon: float) -> tuple[float, float]:
    """Return the coarse coordinate cache key."""
    return round(lat, LOCATION_COORD_DECIMALS), round(lon, LOCATION_COORD_DECIMALS)


def _pick_first(address: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Pick the first non-empty address value for the given keys."""
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalise_geocoder_json(payload: dict[str, Any]) -> dict[str, str]:
    """Extract locality-level fields from reverse-geocoder JSON."""
    address = payload.get("address")
    if not isinstance(address, dict):
        address = {}

    locality = _pick_first(address, _LOCALITY_KEYS)
    if not locality:
        display = str(payload.get("display_name") or "")
        locality = display.split(",", 1)[0].strip() if display else "Unknown"

    region = _pick_first(address, _REGION_KEYS)
    country = str(address.get("country") or "").strip()
    country_code = str(address.get("country_code") or "").strip().lower()
    label = locality or country or "Unknown"

    return {
        "label": label,
        "locality": locality or label,
        "region": region,
        "country": country,
        "country_code": country_code,
    }


def _reverse_geocode_nominatim(lat: float, lon: float) -> dict[str, Any]:
    """Reverse-geocode a coordinate using Nominatim."""
    global _last_request_at

    elapsed = time.monotonic() - _last_request_at
    if elapsed < LOCATION_MIN_REQUEST_INTERVAL_S:
        time.sleep(LOCATION_MIN_REQUEST_INTERVAL_S - elapsed)

    params = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "lat": f"{lat:.7f}",
            "lon": f"{lon:.7f}",
            "zoom": "10",
            "addressdetails": "1",
        }
    )
    request = urllib.request.Request(
        f"{_NOMINATIM_URL}?{params}",
        headers={"User-Agent": LOCATION_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=LOCATION_HTTP_TIMEOUT_S) as response:
        raw = response.read().decode("utf-8")
    _last_request_at = time.monotonic()
    return json.loads(raw)


def _reverse_geocode(lat: float, lon: float) -> tuple[dict[str, Any], str] | None:
    """Reverse-geocode a coordinate with the configured provider."""
    provider = LOCATION_GEOCODER.strip().lower()
    if _geocoder_disabled():
        return None
    if provider != "nominatim":
        logger.warning("Unsupported location geocoder: %s", LOCATION_GEOCODER)
        return None
    try:
        return _reverse_geocode_nominatim(lat, lon), "nominatim"
    except Exception as exc:
        logger.warning("Location reverse geocode failed: %s", exc)
        return None


def _geocoder_disabled() -> bool:
    """Return whether external reverse geocoding is disabled."""
    return LOCATION_GEOCODER.strip().lower() in {"", "off", "none", "disabled"}


def _is_failed_cached(
    conn: sqlite3.Connection,
    lat_round: float,
    lon_round: float,
) -> bool:
    """Check whether a coordinate has a recorded failure."""
    row = conn.execute(
        """
        SELECT 1 FROM location_point_failed
        WHERE lat_round = ? AND lon_round = ?
        """,
        (lat_round, lon_round),
    ).fetchone()
    return row is not None


def _record_failure(
    conn: sqlite3.Connection,
    lat_round: float,
    lon_round: float,
    seen_at: str,
) -> None:
    """Persist (or bump) a reverse-geocode failure so future runs can skip it."""
    conn.execute(
        """
        INSERT INTO location_point_failed (
            lat_round, lon_round, last_attempt_at, attempt_count
        ) VALUES (?, ?, ?, 1)
        ON CONFLICT(lat_round, lon_round) DO UPDATE SET
            last_attempt_at = excluded.last_attempt_at,
            attempt_count   = attempt_count + 1
        """,
        (lat_round, lon_round, seen_at),
    )


def _find_location(
    conn: sqlite3.Connection,
    locality: str,
    region: str,
    country_code: str,
) -> sqlite3.Row | None:
    """Find a canonical location row."""
    return conn.execute(
        """
        SELECT * FROM location
        WHERE locality = ? AND region = ? AND country_code = ?
        """,
        (locality, region, country_code),
    ).fetchone()


def _get_or_create_location(
    conn: sqlite3.Connection,
    fields: dict[str, str],
    *,
    source: str,
    seen_at: str,
) -> int:
    """Return the canonical location id for normalised locality fields."""
    locality = fields["locality"]
    region = fields["region"]
    country_code = fields["country_code"]
    existing = _find_location(conn, locality, region, country_code)
    if existing:
        conn.execute(
            """
            UPDATE location
            SET label = ?, country = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                fields["label"],
                fields["country"],
                seen_at,
                existing["id"],
            ),
        )
        return int(existing["id"])

    cursor = conn.execute(
        """
        INSERT INTO location (
            label, locality, region, country, country_code, source,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fields["label"],
            locality,
            region,
            fields["country"],
            country_code,
            source,
            seen_at,
            seen_at,
        ),
    )
    return int(cursor.lastrowid)


def resolve_workout_location(
    conn: sqlite3.Connection,
    lat: float | None,
    lon: float | None,
    *,
    seen_at: str | None = None,
) -> int | None:
    """Resolve a workout start coordinate to a canonical locality id.

    Args:
        conn: Open SQLite connection.
        lat: First route latitude, or None for non-route workouts.
        lon: First route longitude, or None for non-route workouts.
        seen_at: Timestamp to use for cache metadata.

    Returns:
        The ``location.id`` row, or None if there is no coordinate or geocoder
        lookup is disabled/unavailable.
    """
    if lat is None or lon is None:
        return None

    timestamp = seen_at or datetime.now(timezone.utc).isoformat()
    lat_round, lon_round = _rounded_coord(lat, lon)
    cached = conn.execute(
        """
        SELECT location_id FROM location_point_cache
        WHERE lat_round = ? AND lon_round = ?
        """,
        (lat_round, lon_round),
    ).fetchone()
    if cached:
        return int(cached["location_id"])
    if _geocoder_disabled() or _is_failed_cached(conn, lat_round, lon_round):
        return None

    logger.debug(
        "Resolving workout location %.2f, %.2f via %s",
        lat_round,
        lon_round,
        LOCATION_GEOCODER,
    )
    geocoded = _reverse_geocode(lat_round, lon_round)
    if geocoded is None:
        _record_failure(conn, lat_round, lon_round, timestamp)
        return None

    payload, source = geocoded
    fields = _normalise_geocoder_json(payload)
    location_id = _get_or_create_location(
        conn,
        fields,
        source=source,
        seen_at=timestamp,
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO location_point_cache (
            lat_round, lon_round, location_id, raw_json, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lat_round,
            lon_round,
            location_id,
            json.dumps(payload, sort_keys=True),
            source,
            timestamp,
            timestamp,
        ),
    )
    place = fields["label"]
    country = fields["country"]
    if country and country.lower() not in place.lower():
        place = f"{place}, {country}"
    logger.info(
        "Resolved workout location %.2f, %.2f -> %s", lat_round, lon_round, place
    )
    return location_id


def prefetch_locations(
    conn: sqlite3.Connection,
    coords: list[tuple[float | None, float | None]],
    *,
    seen_at: str | None = None,
    progress_every: int = 25,
) -> None:
    """Resolve a batch of route coordinates, committing each result eagerly.

    Splits the geocoder-bound work out of any enclosing write transaction so
    that long backfills don't hold the SQLite write lock for hours and so that
    each completed lookup is durably cached even if the run is interrupted.

    Args:
        conn: Open database connection (no enclosing ``with conn:`` expected).
        coords: Raw ``(lat, lon)`` pairs from workouts; ``None`` entries and
            duplicates after rounding are filtered out.
        seen_at: Timestamp recorded on new cache rows.
        progress_every: Emit an INFO progress line every N new network lookups.
    """
    if _geocoder_disabled():
        return

    timestamp = seen_at or datetime.now(timezone.utc).isoformat()
    rounded: set[tuple[float, float]] = set()
    for lat, lon in coords:
        if lat is None or lon is None:
            continue
        rounded.add(_rounded_coord(lat, lon))
    if not rounded:
        return

    cached_keys = {
        (row["lat_round"], row["lon_round"])
        for row in conn.execute(
            "SELECT lat_round, lon_round FROM location_point_cache"
        ).fetchall()
    }
    failed_keys = {
        (row["lat_round"], row["lon_round"])
        for row in conn.execute(
            "SELECT lat_round, lon_round FROM location_point_failed"
        ).fetchall()
    }
    to_fetch = sorted(rounded - cached_keys - failed_keys)
    if not to_fetch:
        logger.info("Geocode prefetch: %d coordinate(s) already cached", len(rounded))
        return

    logger.info(
        "Geocode prefetch: %d new coordinate(s) (cached=%d, failed=%d, unique=%d)",
        len(to_fetch),
        len(rounded & cached_keys),
        len(rounded & failed_keys),
        len(rounded),
    )
    started = time.monotonic()
    for index, (lat_round, lon_round) in enumerate(to_fetch, start=1):
        resolve_workout_location(conn, lat_round, lon_round, seen_at=timestamp)
        if index % progress_every == 0 or index == len(to_fetch):
            elapsed = time.monotonic() - started
            rate = index / elapsed if elapsed > 0 else 0.0
            logger.info(
                "Geocoded %d/%d new coordinate(s) (%.2f/s)",
                index,
                len(to_fetch),
                rate,
            )
