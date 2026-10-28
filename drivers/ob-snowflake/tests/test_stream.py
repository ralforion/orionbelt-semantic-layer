"""``fetch_record_batch()``: the result page by page, under one schema."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from ob_snowflake.connection import Connection
from ob_snowflake.cursor import Cursor
from ob_snowflake.exceptions import DataError


@dataclass
class Column:
    """The shape of one ``snowflake.connector`` ResultMetadata entry."""

    name: str
    type_code: int
    precision: int | None = None
    scale: int | None = None


NUMBER_38_0 = Column("N", 0, 38, 0)
TEXT = Column("T", 2)


def _cursor(description: list[Column], pages: list[pa.Table]) -> Cursor:
    native = MagicMock()
    native.cursor.return_value.description = description
    native.cursor.return_value.fetch_arrow_batches.return_value = iter(pages)
    cursor = Connection(native).cursor()
    cursor.execute("SELECT n, t FROM big")
    return cursor


def test_pages_of_different_integer_widths_stream_as_one_schema() -> None:
    """The connector picks each page's integer width from its values."""
    cursor = _cursor(
        [NUMBER_38_0, TEXT],
        [
            pa.table({"N": pa.array([1, 2], type=pa.int8()), "T": ["a", "b"]}),
            pa.table({"N": pa.array([3_000_000_000], type=pa.int64()), "T": ["c"]}),
        ],
    )

    reader = cursor.fetch_record_batch()

    assert reader.schema.field("N").type == pa.int64()
    table = reader.read_all()
    assert table.column("N").to_pylist() == [1, 2, 3_000_000_000]
    assert table.column("T").to_pylist() == ["a", "b", "c"]


def test_a_page_the_first_pages_types_cannot_hold_fails_the_read() -> None:
    """``CAST(1e30 AS INTEGER)`` arrives decimal: no int64 holds it."""
    cursor = _cursor(
        [NUMBER_38_0],
        [
            pa.table({"N": pa.array([1], type=pa.int8())}),
            pa.table({"N": pa.array([Decimal(10**30)], type=pa.decimal128(38, 0))}),
        ],
    )

    reader = cursor.fetch_record_batch()

    with pytest.raises(DataError, match="first page's types"):
        reader.read_all()


def test_an_empty_result_streams_its_described_schema() -> None:
    cursor = _cursor([NUMBER_38_0, TEXT], [])

    reader = cursor.fetch_record_batch()

    assert reader.schema.names == ["N", "T"]
    assert reader.schema.field("N").type == pa.int64()
    assert reader.read_all().num_rows == 0
