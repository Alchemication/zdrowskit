"""Deterministic subjective-feel adjustments for manually-logged activities.

The ``/add`` Telegram flow collects a feel tag (``easy``/``solid``/``hard``/
``wrecked`` for workouts; ``solid``/``ok``/``restless``/``wrecked`` for
sleep) after the user has picked type, duration, and date. The LLM picks a
historical clone for workouts based on the objective signals; this module
then applies bounded multipliers on top so the stored row reflects today's
session rather than a generic typical analog.

Multipliers are deliberately conservative (≤10–15% for HR/kJ, ≤25% for
sleep in-bed padding) to keep synthesised values within a plausible range.
Every call updates the row's ``source_note`` so downstream readers see why
the numbers differ from the clone.
"""

from __future__ import annotations

WORKOUT_FEELS: tuple[str, ...] = ("easy", "solid", "hard", "wrecked")
SLEEP_FEELS: tuple[str, ...] = ("solid", "ok", "restless", "wrecked")

# Multipliers applied on top of the clone row. ``solid`` is the neutral
# baseline — no adjustment. Pace is stored as speed (m/s), so "faster" is a
# multiplier > 1.0.
_WORKOUT_MULTIPLIERS: dict[str, dict[str, float]] = {
    "easy": {"hr": 0.95, "energy": 0.92, "speed": 0.92},
    "solid": {"hr": 1.00, "energy": 1.00, "speed": 1.00},
    "hard": {"hr": 1.06, "energy": 1.08, "speed": 1.05},
    "wrecked": {"hr": 1.03, "energy": 0.97, "speed": 1.00},
}

# `sleep_in_bed_h = sleep_total_h * factor[feel]`. Higher feel_factor means
# the user spent more time awake in bed relative to what they slept.
_SLEEP_IN_BED_FACTORS: dict[str, float] = {
    "solid": 1.03,
    "ok": 1.08,
    "restless": 1.15,
    "wrecked": 1.25,
}

_DEFAULT_SLEEP_IN_BED_FACTOR = 1.08


def apply_workout_feel(clone: dict, feel: str | None) -> tuple[dict, bool]:
    """Return a new clone dict with feel-based multipliers applied.

    Args:
        clone: Cloned workout row from ``find_workout_clone``. Keys match
            ``store._WORKOUT_CLONE_COLUMNS`` plus ``source_note``.
        feel: One of ``WORKOUT_FEELS``, or ``None``/``"solid"`` for no-op.

    Returns:
        Tuple of (adjusted clone dict, feel_adjusted flag). The flag is
        ``True`` when numeric adjustments were applied (i.e. feel is a
        non-neutral value from ``WORKOUT_FEELS``).
    """
    if feel is None or feel == "solid":
        return dict(clone), False

    multipliers = _WORKOUT_MULTIPLIERS.get(feel)
    if multipliers is None:
        return dict(clone), False

    adjusted = dict(clone)
    for col, factor in (
        ("hr_avg", multipliers["hr"]),
        ("hr_max", multipliers["hr"]),
        ("hr_min", multipliers["hr"]),
        ("active_energy_kj", multipliers["energy"]),
        ("intensity_kcal_per_hr_kg", multipliers["energy"]),
        ("gpx_avg_speed_ms", multipliers["speed"]),
        ("gpx_max_speed_p95_ms", multipliers["speed"]),
    ):
        value = adjusted.get(col)
        if value is None:
            continue
        if col in ("hr_max", "hr_min"):
            adjusted[col] = round(value * factor)
        else:
            adjusted[col] = round(value * factor, 2)

    # Distance tracks speed when duration is fixed: faster = further.
    distance = adjusted.get("gpx_distance_km")
    if distance is not None:
        adjusted["gpx_distance_km"] = round(distance * multipliers["speed"], 2)

    note = adjusted.get("source_note") or ""
    suffix = _workout_note_suffix(feel)
    adjusted["source_note"] = f"{note}, {suffix}" if note else suffix
    return adjusted, True


def apply_sleep_feel(sleep_total_h: float, feel: str | None) -> tuple[float, bool]:
    """Return ``sleep_in_bed_h`` derived from reported total and feel.

    Args:
        sleep_total_h: User-reported total sleep hours (kept as-is — that is
            the fact we trust).
        feel: One of ``SLEEP_FEELS``, or ``None`` for the legacy 1.08 default.

    Returns:
        Tuple of (``sleep_in_bed_h`` rounded to 2dp, ``feel_adjusted`` flag).
        The flag is ``True`` when a non-``"ok"`` feel was supplied (``"ok"``
        matches the legacy default so we treat it as no-op for the flag).
    """
    if feel is None:
        return round(sleep_total_h * _DEFAULT_SLEEP_IN_BED_FACTOR, 2), False

    factor = _SLEEP_IN_BED_FACTORS.get(feel, _DEFAULT_SLEEP_IN_BED_FACTOR)
    adjusted = feel in _SLEEP_IN_BED_FACTORS and feel != "ok"
    return round(sleep_total_h * factor, 2), adjusted


def _workout_note_suffix(feel: str) -> str:
    """Human-readable suffix explaining the adjustment in ``source_note``."""
    return {
        "easy": "adjusted down for 'easy' feel",
        "hard": "adjusted up for 'hard' feel",
        "wrecked": "high RPE — 'wrecked' feel",
    }.get(feel, f"adjusted for '{feel}' feel")
