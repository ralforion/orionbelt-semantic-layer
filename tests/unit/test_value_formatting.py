"""Tests for service.value_formatting (shared by UI + API)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.service.value_formatting import (
    format_number,
    format_row,
    is_numeric_type_hint,
    locale_separators,
    parse_number_format,
    to_tsv,
)


class TestParseNumberFormat:
    @pytest.mark.parametrize(
        ("fmt", "expected"),
        [
            (None, (False, -1, False)),
            ("", (False, -1, False)),
            ("#,##0.00", (True, 2, False)),
            ("#,##0", (True, -1, False)),
            ("0.00", (False, 2, False)),
            ("0.00%", (False, 2, True)),
            ("0%", (False, 0, True)),
        ],
    )
    def test_patterns(self, fmt: str | None, expected: tuple[bool, int, bool]) -> None:
        assert parse_number_format(fmt) == expected


class TestLocaleSeparators:
    def test_default_en(self) -> None:
        assert locale_separators("") == (",", ".")
        assert locale_separators("en") == (",", ".")
        assert locale_separators("en-US") == (",", ".")

    def test_german_swaps(self) -> None:
        assert locale_separators("de") == (".", ",")
        assert locale_separators("de-AT") == (".", ",")

    def test_unknown_falls_back_to_en(self) -> None:
        assert locale_separators("zz-ZZ") == (",", ".")


class TestFormatNumber:
    def test_thousands_en(self) -> None:
        assert format_number(1234567.89, "#,##0.00", "en") == "1,234,567.89"

    def test_thousands_de(self) -> None:
        assert format_number(1234567.89, "#,##0.00", "de") == "1.234.567,89"

    def test_percent(self) -> None:
        assert format_number(0.1234, "0.00%", "en") == "12.34%"

    def test_percent_de(self) -> None:
        assert format_number(0.1234, "0.00%", "de") == "12,34%"

    def test_no_format_no_locale(self) -> None:
        # No pattern + no locale-driven separators → plain str().
        assert format_number(42.5, None, "") == "42.5"

    def test_zero_decimals(self) -> None:
        assert format_number(1234.7, "#,##0", "en") == "1,235"


class TestFormatRow:
    def test_numeric_cells_get_formatted_strings(self) -> None:
        out = format_row(
            row=[1234.5, "Germany", None],
            column_names=["Revenue", "Country", "Note"],
            fmt_map={"Revenue": "#,##0.00"},
            type_map={"Revenue": "number", "Country": "string", "Note": "string"},
            locale="de",
        )
        assert out == ["1.234,50", "Germany", None]

    def test_int_detected_as_numeric_without_type_map(self) -> None:
        # No format pattern + no type hint: cell is detected as numeric via
        # ``isinstance``, and ``format_number`` preserves the original type
        # in the early-return path (no float coercion).
        out = format_row(
            row=[42],
            column_names=["X"],
            fmt_map={},
            type_map={},
            locale="en",
        )
        assert out == ["42"]

    def test_bool_not_treated_as_numeric(self) -> None:
        out = format_row(
            row=[True, False],
            column_names=["A", "B"],
            fmt_map={},
            type_map={},
            locale="en",
        )
        assert out == ["True", "False"]

    def test_decimal_cell_with_decimal_type_hint(self) -> None:
        """Postgres / Snowflake return Decimal for NUMERIC/DECIMAL columns.

        Regression: ``isinstance(cell, (int, float))`` missed Decimal AND
        ``type_map["X"] == "number"`` missed "decimal(18, 2)" — both
        branches failed and the cell fell through to ``str(cell)`` with
        no formatting.
        """
        out = format_row(
            row=[Decimal("2045134942.09")],
            column_names=["Revenue"],
            fmt_map={"Revenue": "#,##0.00"},
            type_map={"Revenue": "decimal(18, 2)"},
            locale="de",
        )
        assert out == ["2.045.134.942,09"]

    def test_decimal_cell_without_type_hint(self) -> None:
        out = format_row(
            row=[Decimal("75.00")],
            column_names=["X"],
            fmt_map={"X": "#,##0.00"},
            type_map={},
            locale="de",
        )
        assert out == ["75,00"]

    def test_integer_without_format_keeps_no_decimal_suffix(self) -> None:
        """Regression: int cells with no format pattern shouldn't render as ``"X.0"``.

        Previously ``format_row`` did ``float(cell)`` before delegating to
        ``format_number``, so the early-return path returned ``str(52965.0)``
        — i.e. ``"52965.0"``. Raw-mode integer keys (``Order Key``) showed
        the trailing ``.0`` even though no format pattern was declared.
        """
        out = format_row(
            row=[52965, 100, 0],
            column_names=["Order Key", "Qty", "Z"],
            fmt_map={},
            type_map={"Order Key": "int", "Qty": "bigint", "Z": "number"},
            locale="de",
        )
        assert out == ["52965", "100", "0"]

    def test_bigint_type_hint_treated_as_numeric(self) -> None:
        out = format_row(
            row=[42],
            column_names=["Count"],
            fmt_map={"Count": "#,##0"},
            type_map={"Count": "bigint"},
            locale="en",
        )
        assert out == ["42"]

    def test_unparseable_numeric_falls_back_to_str(self) -> None:
        # type_map says "number" but cell is a string that can't be parsed.
        out = format_row(
            row=["not-a-number"],
            column_names=["X"],
            fmt_map={"X": "#,##0.00"},
            type_map={"X": "number"},
            locale="en",
        )
        assert out == ["not-a-number"]


class TestIsNumericTypeHint:
    @pytest.mark.parametrize(
        "hint",
        [
            "number",
            "int",
            "bigint",
            "smallint",
            "tinyint",
            "float",
            "double",
            "double precision",
            "real",
            "decimal",
            "decimal(18, 2)",
            "numeric(10, 4)",
            "DECIMAL(38,9)",
        ],
    )
    def test_recognises_numeric_hints(self, hint: str) -> None:
        assert is_numeric_type_hint(hint) is True

    @pytest.mark.parametrize(
        "hint",
        [None, "", "string", "varchar(255)", "boolean", "date", "datetime", "timestamp"],
    )
    def test_rejects_non_numeric_hints(self, hint: str | None) -> None:
        assert is_numeric_type_hint(hint) is False


class TestToTsv:
    def test_basic_table(self) -> None:
        body = to_tsv(
            ["A", "B"],
            [["1", "2"], ["3", "4"]],
        )
        assert body == "A\tB\n1\t2\n3\t4\n"

    def test_none_rendered_as_empty_by_default(self) -> None:
        body = to_tsv(["A", "B"], [["x", None]])
        assert body == "A\tB\nx\t\n"

    def test_none_with_explicit_null_token(self) -> None:
        body = to_tsv(["A"], [["x"], [None]], null="NULL")
        assert "NULL" in body

    def test_quotes_cell_with_tab(self) -> None:
        body = to_tsv(["A"], [["x\ty"]])
        assert '"x\ty"' in body

    def test_quotes_cell_with_newline(self) -> None:
        body = to_tsv(["A"], [["x\ny"]])
        assert '"x\ny"' in body

    def test_doubles_internal_quotes(self) -> None:
        # Cell containing a double-quote → wrapped in quotes, internal quotes doubled.
        body = to_tsv(["A"], [['say "hi"']])
        assert '"say ""hi"""' in body

    def test_no_quoting_for_safe_cells(self) -> None:
        body = to_tsv(["A"], [["hello world"]])
        assert body == "A\nhello world\n"
