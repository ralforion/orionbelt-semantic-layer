"""``execute_stream()``: the HTTP response read block by block, not buffered."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa

from ob_clickhouse.cursor import Cursor
from ob_clickhouse.type_codes import NUMBER, STRING

TABLE = pa.table({"n": [1, 2, 3], "s": ["a", "b", "c"]})


class _Stream:
    """``clickhouse_connect``'s StreamContext over an Arrow IPC stream reader."""

    def __init__(self, table: pa.Table) -> None:
        self.gen = pa.RecordBatchReader.from_batches(
            table.schema, table.to_batches(max_chunksize=1)
        )
        self.entered = False
        self.closed = False

    def __enter__(self) -> _Stream:
        self.entered = True
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True


def _client(stream: _Stream) -> MagicMock:
    client = MagicMock()
    client.query_arrow_stream.return_value = stream
    client.query_arrow.return_value = TABLE
    return client


def test_streams_the_response_and_keeps_it_open() -> None:
    stream = _Stream(TABLE)
    client = _client(stream)
    cursor = Cursor(client)

    reader = cursor.execute_stream("SELECT n, s FROM big")

    client.query_arrow_stream.assert_called_once_with("SELECT n, s FROM big")
    client.query_arrow.assert_not_called()
    assert stream.entered and not stream.closed
    assert cursor.description is not None
    assert [(d[0], d[1]) for d in cursor.description] == [("n", NUMBER), ("s", STRING)]
    assert cursor.rowcount == -1
    assert cursor.fetchall() == []
    assert reader.read_all().equals(TABLE)


def test_close_closes_the_response() -> None:
    stream = _Stream(TABLE)
    cursor = Cursor(_client(stream))
    cursor.execute_stream("SELECT n, s FROM big")

    cursor.close()

    assert stream.closed


def test_the_next_statement_closes_the_response() -> None:
    stream = _Stream(TABLE)
    cursor = Cursor(_client(stream))
    cursor.execute_stream("SELECT n, s FROM big")

    cursor.execute("SELECT n, s FROM small")

    assert stream.closed
    assert cursor.fetchall() == [(1, "a"), (2, "b"), (3, "c")]


def test_parameters_are_passed_on() -> None:
    stream = _Stream(TABLE)
    client = _client(stream)

    Cursor(client).execute_stream("SELECT n FROM big WHERE n > {n:Int64}", {"n": 1})

    client.query_arrow_stream.assert_called_once_with(
        "SELECT n FROM big WHERE n > {n:Int64}", parameters={"n": 1}
    )
