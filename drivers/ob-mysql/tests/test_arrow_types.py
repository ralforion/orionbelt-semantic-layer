"""The Arrow schema must come from MySQL's column metadata, not from the rows.

Letting PyArrow infer the types made a column's type a property of whichever
values came back: one result set typed a ``DECIMAL(18, 2)`` column as
``decimal128(3, 2)`` and another typed the same column as ``decimal128(10, 9)``,
an empty result typed every column as string, and an all-NULL column came back
as Arrow's ``null`` type.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pyarrow as pa

from ob_mysql.arrow_types import table_from_rows


def _column(
    name: str,
    type_code: int,
    *,
    flags: int = 0,
    charset: int | None = None,
) -> tuple[object, ...]:
    """One ``cursor.description`` entry, as mysql-connector-python builds it."""
    return (name, type_code, None, None, None, None, 1, flags, charset)


LONG = 3
DOUBLE = 5
DATE = 10
TIME = 11
DATETIME = 12
NEWDECIMAL = 246
BLOB = 252
VAR_STRING = 253
BINARY_CHARSET = 63
UNSIGNED_FLAG = 32
LONGLONG = 8


def _type_of(rows: list[tuple[object, ...]], description: list[tuple[object, ...]]) -> pa.DataType:
    return table_from_rows(rows, description).schema.field(0).type


class TestSchemaDoesNotFollowTheValues:
    def test_decimal_precision_is_stable_across_result_sets(self) -> None:
        """The same column must not change width with the rows it returns."""
        description = [_column("amount", NEWDECIMAL)]
        two_dp = _type_of([(Decimal("2.55"),), (Decimal("1.10"),)], description)
        one_row = _type_of([(Decimal("12345.67"),)], description)
        assert two_dp == one_row == pa.decimal128(38, 2)

    def test_decimal_scale_comes_from_the_declared_scale(self) -> None:
        """MySQL renders a decimal with its column's scale, zeros included.

        ``DECIMAL(18, 2)`` yields ``Decimal('10.00')`` rather than
        ``Decimal('10')``, so the widest scale in the column is the declared
        one -- the only place the scale survives, since the connector drops the
        wire's scale byte.
        """
        description = [_column("amount", NEWDECIMAL)]
        assert _type_of([(Decimal("10.00"),)], description) == pa.decimal128(38, 2)
        assert _type_of([(Decimal("2.500000000"),)], description) == pa.decimal128(38, 9)

    def test_decimal_widens_past_decimal128(self) -> None:
        """MySQL DECIMAL runs to 65 digits; decimal128 stops at 38."""
        wide = Decimal("1" * 40)
        assert _type_of([(wide,)], [_column("amount", NEWDECIMAL)]) == pa.decimal256(76, 0)

    def test_integer_width_comes_from_the_field_type(self) -> None:
        """A small value in an INT column is still an int32."""
        assert _type_of([(42,)], [_column("n", LONG)]) == pa.int32()

    def test_unsigned_flag_decides_signedness(self) -> None:
        """``BIGINT UNSIGNED`` does not fit int64, and the flag says so."""
        description = [_column("big", LONGLONG, flags=UNSIGNED_FLAG)]
        table = table_from_rows([(18446744073709551615,)], description)
        assert table.schema.field(0).type == pa.uint64()
        assert table.column(0)[0].as_py() == 18446744073709551615

    def test_binary_charset_separates_bytes_from_text(self) -> None:
        assert _type_of([(b"x",)], [_column("b", BLOB, charset=BINARY_CHARSET)]) == pa.binary()
        assert _type_of([("x",)], [_column("s", VAR_STRING)]) == pa.string()


class TestEmptyAndNullResults:
    def test_empty_result_keeps_its_column_types(self) -> None:
        """Every column used to come back as string when there were no rows."""
        description = [
            _column("n", LONG),
            _column("d", DATE),
            _column("ts", DATETIME),
            _column("f", DOUBLE),
        ]
        schema = table_from_rows([], description).schema
        assert [field.type for field in schema] == [
            pa.int32(),
            pa.date32(),
            pa.timestamp("us"),
            pa.float64(),
        ]

    def test_all_null_column_keeps_its_type(self) -> None:
        """An all-NULL column used to come back as Arrow's null type."""
        table = table_from_rows([(None,), (None,)], [_column("n", LONG)])
        assert table.schema.field(0).type == pa.int32()
        assert table.column(0).to_pylist() == [None, None]


class TestValuesSurvive:
    def test_time_is_a_duration_not_a_clock_reading(self) -> None:
        """MySQL TIME is a signed interval of up to 838 hours."""
        table = table_from_rows([(datetime.timedelta(seconds=3723),)], [_column("t", TIME)])
        assert table.schema.field(0).type == pa.duration("us")
        assert table.column(0)[0].as_py() == datetime.timedelta(seconds=3723)

    def test_date_only_value_in_a_datetime_column(self) -> None:
        """A DATETIME column can hold a DATE, which Arrow will not take as-is."""
        table = table_from_rows([(datetime.date(2026, 8, 15),)], [_column("ts", DATETIME)])
        assert table.schema.field(0).type == pa.timestamp("us")
        assert table.column(0)[0].as_py() == datetime.datetime(2026, 8, 15, 0, 0)

    def test_short_description_entries_are_tolerated(self) -> None:
        """PEP 249 promises seven entries; mysql-connector-python sends nine."""
        seven = ("n", LONG, None, None, None, None, 1)
        assert table_from_rows([(1,)], [seven]).schema.field(0).type == pa.int32()

    def test_unknown_field_type_falls_back_to_inference(self) -> None:
        table = table_from_rows([("x",)], [_column("mystery", 199)])
        assert table.schema.field(0).type == pa.string()
