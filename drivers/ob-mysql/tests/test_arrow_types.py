"""One Arrow schema for a MySQL result, whichever client read it.

PyMySQL's rows take their schema from the column-definition packet, so a
column's type never follows the values that came back; the ADBC driver's
table is brought to that same schema. Each case here was read off a live
MySQL 8.0 through both clients first (``tests/integration`` asserts them
against one).
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pytest

from ob_mysql.arrow_types import (
    Column,
    coerce_value,
    normalize_adbc,
    table_from_rows,
    type_object_for,
)
from ob_mysql.type_codes import BINARY, DATETIME, NUMBER, STRING

LONG = 3
DOUBLE = 5
DATE = 10
TIME = 11
DATETIME_CODE = 12
YEAR = 13
BIT = 16
NEWDECIMAL = 246
BLOB = 252
VAR_STRING = 253
STRING_CODE = 254
BINARY_CHARSET = 63
UTF8MB4 = 255
UNSIGNED_FLAG = 32
SET_FLAG = 2048
LONGLONG = 8


def _pymysql_field(type_code: int, *, length: int, scale: int = 0, flags: int = 0) -> object:
    return SimpleNamespace(
        name="c",
        type_code=type_code,
        flags=flags,
        charsetnr=BINARY_CHARSET,
        length=length,
        scale=scale,
    )


def _table(rows: list[tuple[object, ...]], column: Column) -> pa.Table:
    return table_from_rows([(coerce_value(r[0], column),) for r in rows], [column])


class TestDecimalsTakeTheDeclaredPrecisionAndScale:
    @pytest.mark.parametrize(
        ("length", "scale", "flags", "precision"),
        [
            (20, 2, 0, 18),  # DECIMAL(18, 2): digits + sign + point
            (11, 4, 0, 9),  # DECIMAL(9, 4)
            (33, 0, 0, 32),  # SUM(INT): no point
            (67, 30, 0, 65),  # DECIMAL(65, 30), MySQL's widest
            (19, 2, UNSIGNED_FLAG, 18),  # unsigned: no sign
        ],
    )
    def test_precision_from_the_display_length(
        self, length: int, scale: int, flags: int, precision: int
    ) -> None:
        field = _pymysql_field(NEWDECIMAL, length=length, scale=scale, flags=flags)
        column = Column.from_pymysql(field)
        assert (column.precision, column.scale) == (precision, scale)

    def test_decimal128_up_to_38_digits_then_decimal256(self) -> None:
        narrow = Column("d", NEWDECIMAL, precision=38, scale=2)
        wide = Column("d", NEWDECIMAL, precision=65, scale=30)
        assert _table([(Decimal("1.50"),)], narrow).schema.field(0).type == pa.decimal128(38, 2)
        widest = Decimal("9" * 35 + "." + "9" * 30)
        table = _table([(widest,)], wide)
        assert table.schema.field(0).type == pa.decimal256(65, 30)
        assert table.column(0)[0].as_py() == widest

    def test_an_empty_result_keeps_the_scale(self) -> None:
        """The scale used to be read off the values, so an empty result had none."""
        column = Column("d", NEWDECIMAL, precision=18, scale=2)
        assert _table([], column).schema == _table([(Decimal("2.55"),)], column).schema


class TestSchemaDoesNotFollowTheValues:
    def test_integer_width_comes_from_the_field_type(self) -> None:
        assert _table([(42,)], Column("n", LONG)).schema.field(0).type == pa.int32()

    def test_unsigned_flag_decides_signedness(self) -> None:
        column = Column("big", LONGLONG, flags=UNSIGNED_FLAG)
        table = _table([(18446744073709551615,)], column)
        assert table.schema.field(0).type == pa.uint64()
        assert table.column(0)[0].as_py() == 18446744073709551615

    def test_binary_charset_separates_bytes_from_text(self) -> None:
        blob = Column("b", BLOB, charset=BINARY_CHARSET)
        text = Column("s", VAR_STRING, charset=UTF8MB4)
        assert _table([(b"x",)], blob).schema.field(0).type == pa.binary()
        assert _table([("x",)], text).schema.field(0).type == pa.string()

    def test_empty_and_all_null_results_keep_their_types(self) -> None:
        columns = [Column("n", LONG), Column("d", DATE), Column("ts", DATETIME_CODE)]
        expected = [pa.int32(), pa.date32(), pa.timestamp("us")]
        assert [f.type for f in table_from_rows([], columns).schema] == expected
        nulls = table_from_rows([(None, None, None)], columns)
        assert [f.type for f in nulls.schema] == expected

    def test_unknown_field_type_falls_back_to_inference(self) -> None:
        assert _table([("x",)], Column("mystery", 199)).schema.field(0).type == pa.string()


class TestPyMySQLValues:
    def test_bit_bytes_are_the_big_endian_integer(self) -> None:
        table = _table([(b"\xaa",), (b"\x01\x00",)], Column("b", BIT, flags=UNSIGNED_FLAG))
        assert table.schema.field(0).type == pa.uint64()
        assert table.column(0).to_pylist() == [170, 256]

    def test_a_zero_date_reads_as_null(self) -> None:
        """PyMySQL hands over the text; ADBC reads it as NULL, and so does this."""
        for code in (DATE, DATETIME_CODE):
            assert coerce_value("0000-00-00", Column("d", code)) is None

    def test_date_only_value_in_a_datetime_column(self) -> None:
        table = _table([(datetime.date(2026, 8, 15),)], Column("ts", DATETIME_CODE))
        assert table.column(0)[0].as_py() == datetime.datetime(2026, 8, 15)

    def test_time_is_a_duration_not_a_clock_reading(self) -> None:
        """MySQL TIME is a signed interval of up to 838 hours."""
        value = -datetime.timedelta(hours=838, minutes=59, seconds=59)
        table = _table([(value,)], Column("t", TIME))
        assert table.schema.field(0).type == pa.duration("us")
        assert table.column(0)[0].as_py() == value

    def test_set_is_one_string(self) -> None:
        column = Column("s", STRING_CODE, flags=SET_FLAG, charset=UTF8MB4)
        assert _table([("x,z",), ({"z", "x"},)], column).column(0).to_pylist() == ["x,z", "x,z"]


def _adbc_field(name: str, arrow_type: pa.DataType, database_type: str) -> pa.Field:
    return pa.field(name, arrow_type, nullable=False).with_metadata(
        {"sql.database_type_name": database_type}
    )


class TestADBCTablesTakeTheSameSchema:
    def test_each_type_the_driver_reads_differently(self) -> None:
        moment = datetime.datetime(2024, 1, 2, 3, 4, 5, 123000)
        fields = [
            _adbc_field("dec9", pa.decimal32(9, 4), "DECIMAL"),
            _adbc_field("dec18", pa.decimal64(18, 2), "DECIMAL"),
            _adbc_field("dec65", pa.decimal256(65, 30), "DECIMAL"),
            _adbc_field("dt_s", pa.timestamp("s"), "DATETIME"),
            _adbc_field("ts_ms", pa.timestamp("ms", tz="UTC"), "TIMESTAMP"),
            _adbc_field("t_s", pa.time32("s"), "TIME"),
            _adbc_field("t_us", pa.time64("us"), "TIME"),
            _adbc_field("y", pa.opaque(pa.string(), "YEAR", "MySQL"), "YEAR"),
            _adbc_field("b", pa.binary(), "BIT"),
            _adbc_field("j", pa.json_(), "JSON"),
        ]
        values = [
            pa.array([Decimal("12.3456")], pa.decimal32(9, 4)),
            pa.array([Decimal("12345.67")], pa.decimal64(18, 2)),
            pa.array([Decimal("1.5")], pa.decimal256(65, 30)),
            pa.array([moment.replace(microsecond=0)], pa.timestamp("s")),
            pa.array([moment], pa.timestamp("ms", tz="UTC")),
            pa.array([datetime.time(1, 2, 3)], pa.time32("s")),
            pa.array([datetime.time(1, 2, 3, 500000)], pa.time64("us")),
            pa.ExtensionArray.from_storage(
                pa.opaque(pa.string(), "YEAR", "MySQL"), pa.array(["2024"])
            ),
            pa.array([b"\xaa"]),
            pa.ExtensionArray.from_storage(pa.json_(), pa.array(['{"k": 1}'])),
        ]
        table = normalize_adbc(pa.Table.from_arrays(values, schema=pa.schema(fields)))
        assert [(f.name, f.type) for f in table.schema] == [
            ("dec9", pa.decimal128(9, 4)),
            ("dec18", pa.decimal128(18, 2)),
            ("dec65", pa.decimal256(65, 30)),
            ("dt_s", pa.timestamp("us")),
            ("ts_ms", pa.timestamp("us")),
            ("t_s", pa.duration("us")),
            ("t_us", pa.duration("us")),
            ("y", pa.int16()),
            ("b", pa.uint64()),
            ("j", pa.string()),
        ]
        assert all(f.nullable and not f.metadata for f in table.schema)
        assert table.to_pylist() == [
            {
                "dec9": Decimal("12.3456"),
                "dec18": Decimal("12345.67"),
                "dec65": Decimal("1.5"),
                "dt_s": moment.replace(microsecond=0),
                # The wall clock MySQL showed in the session, the UTC label dropped.
                "ts_ms": moment,
                "t_s": datetime.timedelta(hours=1, minutes=2, seconds=3),
                "t_us": datetime.timedelta(hours=1, minutes=2, seconds=3, microseconds=500000),
                "y": 2024,
                "b": 170,
                "j": '{"k": 1}',
            }
        ]

    def test_types_both_clients_already_agree_on_pass_through(self) -> None:
        table = pa.table(
            {"n": pa.array([1], pa.int32()), "s": ["x"], "d": [datetime.date(2024, 1, 1)]}
        )
        assert normalize_adbc(table).schema == table.schema


def test_pep_249_type_objects() -> None:
    assert type_object_for(pa.decimal128(9, 2)) is NUMBER
    assert type_object_for(pa.uint64()) is NUMBER
    assert type_object_for(pa.timestamp("us")) is DATETIME
    assert type_object_for(pa.duration("us")) is DATETIME
    assert type_object_for(pa.date32()) is DATETIME
    assert type_object_for(pa.binary()) is BINARY
    assert type_object_for(pa.string()) is STRING
