"""Tests for src/units.py display helpers."""

from types import SimpleNamespace

import pytest

from units import (
    KM_PER_MILE,
    Quantity,
    UnitSystem,
    format_pace,
    format_quantity,
    pace_to_display,
    pace_unit,
    parse_unit_system,
    to_display,
    unit_label,
    units_from_args,
)

M = UnitSystem.METRIC
I = UnitSystem.IMPERIAL  # noqa: E741


class TestParseUnitSystem:
    def test_valid(self):
        assert parse_unit_system("metric") is M
        assert parse_unit_system("imperial") is I

    def test_invalid_lists_choices(self):
        with pytest.raises(ValueError, match="'metric', 'imperial'"):
            parse_unit_system("US")


class TestUnitsFromArgs:
    def test_missing_defaults_metric(self):
        assert units_from_args(SimpleNamespace()) is M

    def test_none_defaults_metric(self):
        assert units_from_args(SimpleNamespace(units=None)) is M

    def test_reads_value(self):
        assert units_from_args(SimpleNamespace(units=I)) is I
        assert units_from_args(SimpleNamespace(units="imperial")) is I


class TestToDisplay:
    @pytest.mark.parametrize("quantity", list(Quantity))
    def test_none_passthrough(self, quantity):
        assert to_display(None, quantity, I) is None

    @pytest.mark.parametrize("quantity", list(Quantity))
    def test_metric_is_identity(self, quantity):
        assert to_display(12.5, quantity, M) == 12.5

    def test_imperial_values(self):
        assert to_display(KM_PER_MILE, Quantity.DISTANCE, I) == pytest.approx(1.0)
        assert to_display(0.3048, Quantity.ELEVATION, I) == pytest.approx(1.0)
        assert to_display(0.3048, Quantity.STRIDE, I) == pytest.approx(1.0)
        assert to_display(2.54, Quantity.STEP_LENGTH, I) == pytest.approx(1.0)
        assert to_display(KM_PER_MILE, Quantity.SPEED, I) == pytest.approx(1.0)
        assert to_display(100, Quantity.TEMPERATURE, I) == pytest.approx(212)
        assert to_display(0, Quantity.TEMPERATURE, I) == pytest.approx(32)
        assert to_display(4.184, Quantity.ENERGY, I) == pytest.approx(1.0)


class TestLabelsAndFormat:
    def test_metric_labels_match_stored_units(self):
        labels = [unit_label(q, M) for q in Quantity]
        assert labels == ["km", "m", "m", "cm", "km/h", "°C", "kJ"]

    def test_imperial_labels(self):
        labels = [unit_label(q, I) for q in Quantity]
        assert labels == ["mi", "ft", "ft", "in", "mph", "°F", "kcal"]

    def test_format_quantity(self):
        assert format_quantity(10.0, Quantity.DISTANCE, M) == "10.0 km"
        assert format_quantity(KM_PER_MILE * 3, Quantity.DISTANCE, I) == "3.0 mi"
        assert format_quantity(5.0, Quantity.DISTANCE, M, decimals=0) == "5 km"
        assert format_quantity(None, Quantity.DISTANCE, I) is None


class TestFormatPace:
    def test_metric(self):
        assert format_pace(5.6166, M) == "5:37/km"

    def test_imperial(self):
        assert format_pace(5.0, I) == "8:03/mi"
        assert pace_to_display(5.0, I) == pytest.approx(8.04672)

    def test_seconds_carry(self):
        assert format_pace(5.9999, M) == "6:00/km"

    def test_suffix_false(self):
        assert format_pace(5.0, M, suffix=False) == "5:00"
        assert format_pace(5.0, I, suffix=False) == "8:03"

    def test_none(self):
        assert format_pace(None, I) is None
        assert pace_to_display(None, I) is None

    def test_pace_unit(self):
        assert pace_unit(M) == "/km"
        assert pace_unit(I) == "/mi"
