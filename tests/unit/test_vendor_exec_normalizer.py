"""Unit tests for the vendor-exec row normaliser.

The vendor-exec sweep compares row sets across DB engines by routing
every cell through ``_normalize_value``. The numeric-string coercion
step inside it must not collapse distinct string keys (zero-padded
IDs, exponent-form strings) — otherwise key-handling regressions
across vendors would silently green the test.

These tests pin that behaviour at the unit level so it is enforced
without standing up testcontainers.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

pytest.importorskip("duckdb", reason="duckdb required to import vendor_exec")

from tests.integration.drift.vendor_exec.test_vendor_exec import _normalize_value


class TestNormalizeValueStringCoercion:
    def test_canonical_decimal_string_is_normalised(self) -> None:
        # Goldens persist Decimals as strings — these still need to
        # collapse to the same canonical numeric form so they compare
        # equal to a Decimal coming back from a live vendor.
        assert _normalize_value("100.50") == "100.5"
        assert _normalize_value("0") == "0"
        assert _normalize_value("-5") == "-5"

    def test_zero_padded_id_is_preserved(self) -> None:
        # SKU codes / order IDs that look numeric must NOT be collapsed
        # to their integer form — otherwise "00123" and "123" would
        # compare equal and mask a vendor key-handling bug.
        assert _normalize_value("00123") == "00123"
        assert _normalize_value("0000") == "0000"
        assert _normalize_value("01") == "01"

    def test_scientific_notation_string_is_preserved(self) -> None:
        # "1e3" as a string is a legitimate distinct value (e.g. a
        # product code) and should not be coerced into 1000.0.
        assert _normalize_value("1e3") == "1e3"
        assert _normalize_value("1E3") == "1E3"

    def test_non_numeric_string_passes_through(self) -> None:
        assert _normalize_value("Germany") == "Germany"
        assert _normalize_value("") == ""

    def test_decimal_value_is_normalised(self) -> None:
        assert _normalize_value(Decimal("100.50")) == "100.5"

    def test_float_value_is_normalised(self) -> None:
        assert _normalize_value(3.14159265358979) == "3.1415926536"

    def test_none_passes_through(self) -> None:
        assert _normalize_value(None) is None

    def test_iso_date_passes_through(self) -> None:
        d = _dt.date(2026, 5, 10)
        assert _normalize_value(d) == "2026-05-10"
