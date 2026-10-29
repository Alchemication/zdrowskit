"""Physical unit-conversion constants shared across the project.

No project imports, so any module can use this without an import cycle.
Storage is always metric; this module converts stored values for display.
"""

from enum import StrEnum

KM_PER_MILE: float = 1.609344
"""Kilometres in one international mile (exact by definition)."""

M_PER_FOOT: float = 0.3048
"""Metres in one international foot (exact by definition)."""

M_PER_YARD: float = 0.9144
"""Metres in one international yard (exact by definition)."""

CM_PER_INCH: float = 2.54
"""Centimetres in one international inch (exact by definition)."""

KJ_PER_KCAL: float = 4.184
"""Kilojoules in one kilocalorie (thermochemical calorie, as Apple defines it)."""

MS_PER_MPH: float = 0.44704
"""Metres per second in one mile per hour (exact by definition)."""


class UnitSystem(StrEnum):
    """Display unit system for a profile. Storage is always metric."""

    METRIC = "metric"
    IMPERIAL = "imperial"


class Quantity(StrEnum):
    """Physical quantities that have a metric and an imperial display unit."""

    DISTANCE = "distance"
    ELEVATION = "elevation"
    STRIDE = "stride"
    STEP_LENGTH = "step_length"
    SPEED = "speed"
    TEMPERATURE = "temperature"
    ENERGY = "energy"


_LABELS: dict[Quantity, tuple[str, str]] = {
    Quantity.DISTANCE: ("km", "mi"),
    Quantity.ELEVATION: ("m", "ft"),
    Quantity.STRIDE: ("m", "ft"),
    Quantity.STEP_LENGTH: ("cm", "in"),
    Quantity.SPEED: ("km/h", "mph"),
    Quantity.TEMPERATURE: ("°C", "°F"),
    Quantity.ENERGY: ("kJ", "kcal"),
}
"""(metric, imperial) display label per quantity, in the stored unit first."""


def parse_unit_system(raw: object) -> UnitSystem:
    """Parse a user-supplied unit-system name.

    Args:
        raw: Value such as ``"metric"`` or ``"imperial"``.

    Returns:
        The matching ``UnitSystem``.

    Raises:
        ValueError: If ``raw`` is not a known unit system.
    """
    try:
        return UnitSystem(raw)
    except ValueError:
        choices = ", ".join(repr(u.value) for u in UnitSystem)
        raise ValueError(f"units must be one of {choices}, got {raw!r}.") from None


def units_from_args(args: object) -> UnitSystem:
    """Return the display unit system carried on a CLI/runner args object.

    Args:
        args: Namespace-like object; a missing or None ``units`` means metric.

    Returns:
        The unit system, defaulting to metric.
    """
    raw = getattr(args, "units", None)
    return UnitSystem.METRIC if raw is None else parse_unit_system(raw)


def unit_label(quantity: Quantity, units: UnitSystem) -> str:
    """Return the display label (``km``, ``mi``, ...) for a quantity."""
    metric, imperial = _LABELS[quantity]
    return imperial if units is UnitSystem.IMPERIAL else metric


def to_display(
    value: float | None, quantity: Quantity, units: UnitSystem
) -> float | None:
    """Convert a stored metric value to the display unit system.

    Args:
        value: Value in the stored unit (km, m, cm, km/h, degC, kJ), or None.
        quantity: Which physical quantity the value is.
        units: Target unit system.

    Returns:
        The converted value, or None if ``value`` is None.
    """
    if value is None or units is UnitSystem.METRIC:
        return value
    if quantity is Quantity.DISTANCE:
        return value / KM_PER_MILE
    if quantity in (Quantity.ELEVATION, Quantity.STRIDE):
        return value / M_PER_FOOT
    if quantity is Quantity.STEP_LENGTH:
        return value / CM_PER_INCH
    if quantity is Quantity.SPEED:
        return value / KM_PER_MILE
    if quantity is Quantity.TEMPERATURE:
        return value * 9 / 5 + 32
    return value / KJ_PER_KCAL


def format_quantity(
    value: float | None,
    quantity: Quantity,
    units: UnitSystem,
    *,
    decimals: int = 1,
) -> str | None:
    """Format a stored metric value as ``"12.3 mi"`` in the display units.

    Args:
        value: Value in the stored unit, or None.
        quantity: Which physical quantity the value is.
        units: Target unit system.
        decimals: Digits after the decimal point.

    Returns:
        The formatted string, or None if ``value`` is None.
    """
    shown = to_display(value, quantity, units)
    if shown is None:
        return None
    return f"{shown:.{decimals}f} {unit_label(quantity, units)}"


def pace_to_display(min_per_km: float | None, units: UnitSystem) -> float | None:
    """Convert a stored min/km pace to min per display distance unit."""
    if min_per_km is None:
        return None
    return min_per_km * KM_PER_MILE if units is UnitSystem.IMPERIAL else min_per_km


def pace_unit(units: UnitSystem) -> str:
    """Return the pace suffix: ``/km`` or ``/mi``."""
    return "/mi" if units is UnitSystem.IMPERIAL else "/km"


def format_pace(
    min_per_km: float | None, units: UnitSystem, *, suffix: bool = True
) -> str | None:
    """Format a stored min/km pace as ``m:ss/km`` or ``m:ss/mi``.

    Args:
        min_per_km: Pace in minutes per kilometre, or None.
        units: Target unit system.
        suffix: Append the ``/km`` or ``/mi`` suffix.

    Returns:
        The formatted pace, or None if ``min_per_km`` is None.
    """
    pace = pace_to_display(min_per_km, units)
    if pace is None:
        return None
    total = int(round(pace * 60))
    text = f"{total // 60}:{total % 60:02d}"
    return text + pace_unit(units) if suffix else text
