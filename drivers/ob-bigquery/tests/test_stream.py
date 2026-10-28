"""``execute_stream()``: the result downloaded page by page, not buffered."""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from ob_bigquery.cursor import Cursor
from ob_bigquery.exceptions import DataError


def _native(pages: list[pa.RecordBatch], empty: pa.Table | None = None) -> MagicMock:
    native = MagicMock()
    job = native.query.return_value
    job.result.return_value.to_arrow_iterable.return_value = iter(pages)
    job.to_arrow.return_value = empty
    return native


INT64 = pa.int64()


def _batch(values: list[object], kind: pa.DataType = INT64) -> pa.RecordBatch:
    return pa.record_batch([pa.array(values, type=kind)], names=["n"])


def test_streams_the_pages_without_buffering() -> None:
    native = _native([_batch([1, 2]), _batch([3])])
    cursor = Cursor(native)

    reader = cursor.execute_stream("SELECT n FROM big")

    native.query.assert_called_once_with("SELECT n FROM big")
    native.query.return_value.to_arrow.assert_not_called()
    assert reader.read_all().column("n").to_pylist() == [1, 2, 3]
    assert cursor.fetchall() == []


def test_a_later_page_is_held_to_the_first_pages_types() -> None:
    cursor = Cursor(_native([_batch([1], pa.int32()), _batch([2])]))

    reader = cursor.execute_stream("SELECT n FROM big")

    assert reader.schema.field("n").type == pa.int32()
    assert reader.read_all().column("n").to_pylist() == [1, 2]


def test_a_page_the_first_pages_types_cannot_hold_fails_the_read() -> None:
    cursor = Cursor(_native([_batch([1], pa.int8()), _batch([1000])]))

    reader = cursor.execute_stream("SELECT n FROM big")

    with pytest.raises(DataError, match="first page's types"):
        reader.read_all()


def test_an_empty_result_streams_its_schema() -> None:
    empty = pa.table({"n": pa.array([], type=pa.int64())})
    cursor = Cursor(_native([], empty))

    reader = cursor.execute_stream("SELECT n FROM big WHERE FALSE")

    assert reader.schema == empty.schema
    assert reader.read_all().num_rows == 0


def test_parameters_are_bound_by_name() -> None:
    native = _native([_batch([1])])

    Cursor(native).execute_stream("SELECT n FROM big WHERE n > @n", {"n": 0})

    config = native.query.call_args.kwargs["job_config"]
    assert [(p.name, p.type_, p.value) for p in config.query_parameters] == [("n", "INT64", 0)]
