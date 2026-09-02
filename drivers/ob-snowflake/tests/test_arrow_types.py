"""The schema comes from the description, not from the values that arrived."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pyarrow as pa

from ob_snowflake.arrow_types import stable_arrow_table


@dataclass
class Column:
    """The shape of one ``snowflake.connector`` ResultMetadata entry."""

    name: str
    type_code: int
    precision: int | None = None
    scale: int | None = None


NUMBER_38_0 = Column("N", 0, 38, 0)
NUMBER_18_2 = Column("D", 0, 18, 2)
TEXT = Column("T", 2)


def test_a_narrow_integer_is_widened_to_the_declared_kind() -> None:
    """``CAST(42 AS INTEGER)`` arrives ``int8``; the column is NUMBER(38, 0).

    Which is the whole defect: the connector reads the width off the values in
    the batch, so a second page holding 3000000000 answers ``int64`` for the
    same column, and a consumer that trusted the first page is wrong.
    """
    table = pa.table({"N": pa.array([42], type=pa.int8())})
    out = stable_arrow_table(table, [NUMBER_38_0])
    assert out.schema.field(0).type == pa.int64()
    assert out.column(0)[0].as_py() == 42


def test_an_integer_already_wide_is_left_as_it_is() -> None:
    table = pa.table({"N": pa.array([3000000000], type=pa.int64())})
    out = stable_arrow_table(table, [NUMBER_38_0])
    assert out.schema.field(0).type == pa.int64()


def test_a_number_too_wide_for_an_integer_stays_decimal() -> None:
    """``CAST(1e30 AS INTEGER)`` arrives ``decimal128(38, 0)``.

    Widening that to ``int64`` would overflow a value the warehouse holds
    legally, so the column keeps the type the connector chose.
    """
    table = pa.table({"N": pa.array([Decimal("1" + "0" * 30)], type=pa.decimal128(38, 0))})
    out = stable_arrow_table(table, [NUMBER_38_0])
    assert out.schema.field(0).type == pa.decimal128(38, 0)


def test_a_scaled_number_is_untouched() -> None:
    """Only integers are read from the values; a decimal already says its width."""
    table = pa.table({"D": pa.array([Decimal("2.55")], type=pa.decimal128(38, 2))})
    out = stable_arrow_table(table, [NUMBER_18_2])
    assert out.schema.field(0).type == pa.decimal128(38, 2)


def test_a_non_numeric_column_is_untouched() -> None:
    table = pa.table({"T": pa.array(["x"])})
    assert stable_arrow_table(table, [TEXT]).schema.field(0).type == pa.string()


def test_an_empty_result_answers_a_schema() -> None:
    """The connector answers ``None`` for a result with no rows.

    A consumer asking an empty result for its columns should get them rather
    than an exception, and the declared kinds are known even when no value is.
    """
    out = stable_arrow_table(None, [NUMBER_38_0, NUMBER_18_2, TEXT])
    assert out is not None
    assert out.num_rows == 0
    assert [f.type for f in out.schema] == [
        pa.int64(),
        pa.decimal128(18, 2),
        pa.string(),
    ]


def test_no_description_leaves_the_table_alone() -> None:
    """Nothing to read the widths from, so nothing is claimed about them."""
    table = pa.table({"N": pa.array([42], type=pa.int8())})
    assert stable_arrow_table(table, None).schema.field(0).type == pa.int8()


def test_a_description_that_does_not_line_up_is_ignored() -> None:
    """Best effort: a loose schema beats a positional cast onto the wrong column."""
    table = pa.table({"N": pa.array([42], type=pa.int8())})
    assert stable_arrow_table(table, [NUMBER_38_0, TEXT]).schema.field(0).type == pa.int8()


def test_two_columns_of_one_name_both_survive() -> None:
    """``SELECT a AS X, b AS X`` is an ordinary result, and a dict keeps one.

    Reached only on the empty path, where the schema is built rather than
    carried over from the connector's own table.
    """
    out = stable_arrow_table(None, [Column("X", 2), Column("X", 0, 38, 0)])
    assert out is not None
    assert out.num_columns == 2
    assert [f.name for f in out.schema] == ["X", "X"]
    assert [f.type for f in out.schema] == [pa.string(), pa.int64()]
