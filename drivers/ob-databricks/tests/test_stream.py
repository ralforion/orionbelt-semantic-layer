"""``fetch_record_batch()``: the result page by page, under one schema."""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow as pa

from ob_databricks.arrow_stream import BATCH_ROWS
from ob_databricks.connection import Connection
from ob_databricks.cursor import Cursor


def _cursor(pages: list[pa.Table]) -> tuple[Cursor, MagicMock]:
    native = MagicMock()
    native.cursor.return_value.fetchmany_arrow.side_effect = pages
    cursor = Connection(native).cursor()
    cursor.execute("SELECT n FROM big")
    return cursor, native.cursor.return_value


def test_pages_until_an_empty_one() -> None:
    schema = pa.schema([("n", pa.int64())])
    cursor, native = _cursor(
        [
            pa.table({"n": [1, 2]}, schema=schema),
            pa.table({"n": [3]}, schema=schema),
            pa.table({"n": []}, schema=schema),
        ]
    )

    table = cursor.fetch_record_batch().read_all()

    assert table.column("n").to_pylist() == [1, 2, 3]
    assert native.fetchmany_arrow.call_count == 3
    native.fetchmany_arrow.assert_called_with(BATCH_ROWS)
    native.fetchall_arrow.assert_not_called()


def test_a_later_page_is_held_to_the_first_pages_types() -> None:
    cursor, _ = _cursor(
        [
            pa.table({"n": pa.array([1], type=pa.int32())}),
            pa.table({"n": pa.array([2], type=pa.int64())}),
            pa.table({"n": pa.array([], type=pa.int32())}),
        ]
    )

    reader = cursor.fetch_record_batch()

    assert reader.schema.field("n").type == pa.int32()
    assert reader.read_all().column("n").to_pylist() == [1, 2]


def test_an_empty_result_streams_its_schema() -> None:
    cursor, _ = _cursor([pa.table({"n": pa.array([], type=pa.int64())})])

    reader = cursor.fetch_record_batch()

    assert reader.schema == pa.schema([("n", pa.int64())])
    assert reader.read_all().num_rows == 0
