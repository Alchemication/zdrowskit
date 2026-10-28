"""Tests for src/parsers/normalise.py."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from parsers.metrics import METRIC_MAP
from parsers.normalise import (
    CONVERSIONS,
    INVARIANT_CANONICALS,
    UnitNormaliser,
    to_canonical,
)


class TestToCanonical:
    @pytest.mark.parametrize(
        ("value", "unit", "canonical", "expected"),
        [
            (1, "mi", "km", 1.609344),
            (1000, "m", "km", 1.0),
            (1760, "yd", "km", 1.609344),
            (5280, "ft", "km", 1.609344),
            (1, "ft", "m", 0.3048),
            (100, "in", "m", 2.54),
            (1, "yd", "m", 0.9144),
            (1, "mi", "m", 1609.344),
            (2, "km", "m", 2000.0),
            (150, "cm", "m", 1.5),
            (1, "in", "cm", 2.54),
            (1, "ft", "cm", 30.48),
            (10, "mm", "cm", 1.0),
            (1.5, "m", "cm", 150.0),
            (1, "mph", "km/h", 1.609344),
            (1, "mi/hr", "km/h", 1.609344),
            (10, "m/s", "km/h", 36.0),
            (12, "kmph", "km/h", 12.0),
            (1, "mph", "m/s", 0.44704),
            (1, "ft/s", "m/s", 0.3048),
            (36, "km/hr", "m/s", 10.0),
            (1, "kcal", "kJ", 4.184),
            (1, "Cal", "kJ", 4.184),
            (1000, "cal", "kJ", 4.184),
            (212, "degF", "degC", 100.0),
            (32, "degF", "degC", 0.0),
            (273.15, "K", "degC", 0.0),
            (1, "hr", "min", 60.0),
            (120, "s", "min", 2.0),
            (60, "min", "hr", 1.0),
            (150, "bpm", "count/min", 150.0),
            (8, "MET", "kcal/hr·kg", 8.0),
            (45, "ml/(kg·min)", "mL/min·kg", 45.0),
        ],
    )
    def test_known_values(
        self, value: float, unit: str, canonical: str, expected: float
    ) -> None:
        assert to_canonical(value, unit, canonical) == pytest.approx(expected)

    def test_unknown_canonical_raises(self) -> None:
        with pytest.raises(KeyError):
            to_canonical(1.0, "km", "furlong")


class TestUnknownUnit:
    def test_unrecognised_label_is_none(self) -> None:
        assert to_canonical(1.0, "furlong", "km") is None

    @pytest.mark.parametrize("canonical", sorted(INVARIANT_CANONICALS))
    def test_invariant_canonical_keeps_value(self, canonical: str) -> None:
        assert to_canonical(5.0, "furlong", canonical) == 5.0

    def test_lookup_is_case_sensitive(self) -> None:
        assert to_canonical(1.0, "KJ", "kJ") is None
        assert to_canonical(1.0, "Kj", "kJ") is None


class TestMissingUnit:
    @pytest.mark.parametrize("canonical", ["km", "kJ", "degC", "min"])
    def test_ambiguous_canonicals_drop_the_value(self, canonical: str) -> None:
        assert to_canonical(5.0, None, canonical) is None

    @pytest.mark.parametrize("canonical", ["count", "count/min", "%"])
    def test_invariant_canonicals_keep_the_value(self, canonical: str) -> None:
        assert to_canonical(5.0, None, canonical) == 5.0


class TestUnitNormaliser:
    def test_warns_once_per_field_and_unit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        norm = UnitNormaliser()
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            for value in (1.0, 2.0, 3.0):
                assert norm.convert(value, "furlong", "km", field="distance") is None
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "distance" in message
        assert "furlong" in message

    def test_invariant_unknown_unit_keeps_value_and_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        norm = UnitNormaliser()
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert norm.convert(45.0, "ml/kg·min", "mL/min·kg", field="vo2") == 45.0
            assert norm.convert(46.0, "ml/kg·min", "mL/min·kg", field="vo2") == 46.0
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "assumed to be mL/min·kg" in message
        assert "ml/kg·min" in message

    def test_missing_unit_renders_as_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert UnitNormaliser().convert(1.0, None, "km", field="d") is None
        assert "(missing)" in caplog.records[0].getMessage()
        assert "None" not in caplog.records[0].getMessage()

    def test_missing_unit_on_invariant_is_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert UnitNormaliser().convert(1.0, None, "count", field="d") == 1.0
        assert caplog.records == []

    @pytest.mark.parametrize("bad", [65, "65", [65]])
    def test_qty_non_dict_is_none(self, bad: object) -> None:
        assert UnitNormaliser().qty(bad, "%", field="h") is None

    def test_distinct_fields_warn_separately(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        norm = UnitNormaliser()
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            norm.convert(1.0, "furlong", "km", field="a")
            norm.convert(1.0, "furlong", "km", field="b")
        assert len(caplog.records) == 2

    def test_good_unit_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert UnitNormaliser().convert(1.0, "mi", "km", field="d") == (
                pytest.approx(1.609344)
            )
        assert caplog.records == []

    def test_qty_none_object_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert UnitNormaliser().qty(None, "km", field="d") is None
        assert caplog.records == []

    def test_qty_missing_value_is_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="parsers.normalise"):
            assert UnitNormaliser().qty({"units": "mi"}, "km", field="d") is None
        assert caplog.records == []

    def test_qty_converts(self) -> None:
        obj = {"qty": 2, "units": "mi"}
        assert UnitNormaliser().qty(obj, "km", field="d") == pytest.approx(3.218688)


class TestTableCoverage:
    def test_every_metric_map_unit_is_convertible(self) -> None:
        for name, spec in METRIC_MAP.items():
            assert spec.unit in CONVERSIONS, name

    def test_stored_unit_is_accepted_as_input(self) -> None:
        for canonical, table in CONVERSIONS.items():
            assert canonical in table

    def test_every_canonical_used_in_workouts_is_convertible(self) -> None:
        """Scrape the string literals passed to norm.qty/_extract_bins."""
        source = (
            Path(__file__).parent.parent / "src" / "parsers" / "workouts.py"
        ).read_text()
        used: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "qty":
                arg = node.args[1]
                assert isinstance(arg, ast.Constant)
                used.add(arg.value)
            for kw in node.keywords:
                if kw.arg == "canonical" and isinstance(kw.value, ast.Constant):
                    used.add(kw.value.value)
        assert used, "scraper found nothing; update this test"
        assert used <= set(CONVERSIONS)
