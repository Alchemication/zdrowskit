"""Normalise Auto Export unit labels to the units zdrowskit stores.

Auto Export labels every value with the unit the phone is configured to export
in, so a phone set to miles sends ``mi`` where a metric one sends ``km``. The
parsers store one fixed unit per field; this module converts whatever label
arrives into that unit, and refuses to guess when it cannot.

``CONVERSIONS`` is the single table of accepted spellings: canonical (stored)
unit -> input unit -> converter. Lookups are exact and case-sensitive, because
``Cal`` (kilocalorie) and ``cal`` (calorie) differ by a factor of 1000.

Public API:
    CONVERSIONS      -- canonical unit -> accepted input unit -> converter
    to_canonical()   -- convert one value
    UnitNormaliser   -- per-payload converter that warns once per bad field/unit
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from units import (
    CM_PER_INCH,
    KJ_PER_KCAL,
    KM_PER_MILE,
    M_PER_FOOT,
    M_PER_YARD,
    MS_PER_MPH,
)

logger = logging.getLogger(__name__)

Converter = Callable[[float], float]


def _scale(factor: float) -> Converter:
    """Return a converter that multiplies by ``factor``."""
    return lambda value: value * factor


def _identity(value: float) -> float:
    """Return ``value`` unchanged, for units that already match the stored one."""
    return value


CONVERSIONS: dict[str, dict[str, Converter]] = {
    "km": {
        "km": _identity,
        "m": _scale(0.001),
        "mi": _scale(KM_PER_MILE),
        "yd": _scale(M_PER_YARD / 1000),
        "ft": _scale(M_PER_FOOT / 1000),
    },
    "m": {
        "m": _identity,
        "km": _scale(1000),
        "cm": _scale(0.01),
        "ft": _scale(M_PER_FOOT),
        "in": _scale(CM_PER_INCH / 100),
        "yd": _scale(M_PER_YARD),
        "mi": _scale(1000 * KM_PER_MILE),
    },
    "cm": {
        "cm": _identity,
        "m": _scale(100),
        "mm": _scale(0.1),
        "in": _scale(CM_PER_INCH),
        "ft": _scale(100 * M_PER_FOOT),
    },
    "km/h": {
        "km/h": _identity,
        "km/hr": _identity,
        "kmph": _identity,
        "m/s": _scale(3.6),
        "mi/hr": _scale(KM_PER_MILE),
        "mph": _scale(KM_PER_MILE),
    },
    "m/s": {
        "m/s": _identity,
        "km/h": _scale(1 / 3.6),
        "km/hr": _scale(1 / 3.6),
        "kmph": _scale(1 / 3.6),
        "ft/s": _scale(M_PER_FOOT),
        "mi/hr": _scale(MS_PER_MPH),
        "mph": _scale(MS_PER_MPH),
    },
    "kJ": {
        "kJ": _identity,
        "kcal": _scale(KJ_PER_KCAL),
        "Cal": _scale(KJ_PER_KCAL),
        "cal": _scale(KJ_PER_KCAL / 1000),
    },
    "degC": {
        "degC": _identity,
        "degF": lambda f: (f - 32) * 5 / 9,
        "K": lambda k: k - 273.15,
    },
    "min": {
        "min": _identity,
        "hr": _scale(60),
        "s": _scale(1 / 60),
    },
    "hr": {
        "hr": _identity,
        "min": _scale(1 / 60),
    },
    "count": {"count": _identity},
    "count/min": {"count/min": _identity, "bpm": _identity},
    "ms": {"ms": _identity},
    "%": {"%": _identity},
    "W": {"W": _identity},
    "mL/min·kg": {
        "mL/min·kg": _identity,
        "ml/(kg·min)": _identity,
        "mL/(kg·min)": _identity,
    },
    # MET is kcal/(hr·kg) by definition, so Auto Export's MET label is a rename.
    "kcal/hr·kg": {
        "kcal/hr·kg": _identity,
        "kcal/(kg·hr)": _identity,
        "kcal/(hr·kg)": _identity,
        "MET": _identity,
    },
}

# Canonicals with no locale variant. A value with no unit label, or one this
# table does not recognise, is assumed to already be in these (and an unknown
# label is warned about); for every other canonical the label is ambiguous
# (km or mi?), so the value is dropped instead.
INVARIANT_CANONICALS: frozenset[str] = frozenset(
    {"count", "count/min", "ms", "%", "W", "mL/min·kg", "kcal/hr·kg"}
)


def to_canonical(value: float, unit: str | None, canonical: str) -> float | None:
    """Convert ``value`` from ``unit`` into the ``canonical`` stored unit.

    Args:
        value: The number as sent by Auto Export.
        unit: The unit label sent with it, or None when absent.
        canonical: The unit zdrowskit stores, a key of ``CONVERSIONS``.

    Returns:
        The converted value. For a canonical in ``INVARIANT_CANONICALS`` a
        missing or unrecognised ``unit`` returns ``value`` as-is; otherwise
        None.

    Raises:
        KeyError: ``canonical`` is not in ``CONVERSIONS`` (a programmer error).
    """
    table = CONVERSIONS[canonical]
    converter = table.get(unit) if unit is not None else None
    if converter is not None:
        return converter(value)
    return value if canonical in INVARIANT_CANONICALS else None


class UnitNormaliser:
    """Converts values for one payload parse, warning once per bad field/unit.

    A 50-workout payload with one unrecognised label would otherwise log 50
    identical warnings, so each ``(field, unit)`` pair is reported once.
    """

    def __init__(self) -> None:
        self._warned: set[tuple[str, str | None]] = set()

    def convert(
        self, value: float, unit: str | None, canonical: str, *, field: str
    ) -> float | None:
        """Convert one value, logging a warning if its unit is unusable.

        Args:
            value: The number as sent.
            unit: Its unit label, or None.
            canonical: The stored unit, a key of ``CONVERSIONS``.
            field: Name used in the warning, e.g. a metric or workout field.

        Returns:
            The converted value, or None if the unit is unusable. For a
            single-unit canonical an unrecognised unit keeps the value (warned).
        """
        result = to_canonical(value, unit, canonical)
        # A missing label on a single-unit canonical is normal, not worth a log.
        if result is None or (unit is not None and unit not in CONVERSIONS[canonical]):
            self.warn(field, unit, canonical)
        return result

    def qty(self, obj: object, canonical: str, *, field: str) -> float | None:
        """Extract and convert the ``{"qty", "units"}`` pair of a workout field.

        Args:
            obj: A dict like ``{"qty": 81.6, "units": "count/min"}``. Anything
                else, including None, yields None.
            canonical: The stored unit, a key of ``CONVERSIONS``.
            field: Name used in the warning.

        Returns:
            The converted value, or None if obj is None, "qty" is absent (no
            warning), or the unit is unusable (warned once).
        """
        if not isinstance(obj, dict):
            return None
        raw = obj.get("qty")
        if raw is None:
            return None
        return self.convert(float(raw), obj.get("units"), canonical, field=field)

    def accepts(self, unit: str | None, canonical: str, *, field: str) -> bool:
        """Return whether ``unit`` converts to ``canonical``, warning if not."""
        return self.convert(1.0, unit, canonical, field=field) is not None

    def warn(self, field: str, unit: str | None, canonical: str) -> None:
        """Log that ``field`` has an unusable ``unit``, once per pair."""
        key = (field, unit)
        if key in self._warned:
            return
        self._warned.add(key)
        outcome = (
            f"assumed to be {canonical}, value kept"
            if canonical in INVARIANT_CANONICALS
            else "value not stored"
        )
        logger.warning(
            "Unit %s for %s is not a recognised %s unit (accepted: %s); %s.",
            "(missing)" if unit is None else repr(unit),
            field,
            canonical,
            ", ".join(sorted(CONVERSIONS[canonical])),
            outcome,
        )
