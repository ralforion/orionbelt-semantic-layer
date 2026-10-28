"""``execute_stream()``: the Flight stream read as it is consumed, not buffered."""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from ob_dremio.cursor import Cursor
from ob_dremio.type_codes import NUMBER, STRING

TABLE = pa.table({"n": [1, 2, 3], "s": ["a", "b", "c"]})


def _native(table: pa.Table = TABLE) -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    statement = connection.cursor.return_value
    statement.fetch_record_batch.return_value = pa.RecordBatchReader.from_batches(
        table.schema, table.to_batches(max_chunksize=1)
    )
    return connection, statement


def test_streams_the_native_reader_and_keeps_its_statement_open() -> None:
    connection, statement = _native()
    cursor = Cursor(connection)

    reader = cursor.execute_stream("SELECT n, s FROM big")

    statement.execute.assert_called_once_with("SELECT n, s FROM big")
    statement.fetch_arrow_table.assert_not_called()
    statement.close.assert_not_called()
    assert cursor.description is not None
    assert [(d[0], d[1]) for d in cursor.description] == [("n", NUMBER), ("s", STRING)]
    assert cursor.rowcount == -1
    assert cursor.fetchall() == []
    assert reader.read_all().equals(TABLE)


def test_close_closes_the_statement_the_stream_reads_from() -> None:
    connection, statement = _native()
    cursor = Cursor(connection)
    cursor.execute_stream("SELECT n, s FROM big")

    cursor.close()

    statement.close.assert_called_once()


def test_the_next_statement_closes_the_streams() -> None:
    connection, statement = _native()
    cursor = Cursor(connection)
    cursor.execute_stream("SELECT n, s FROM big")
    statement.fetch_arrow_table.return_value = TABLE

    cursor.execute("SELECT 1")

    # The stream's statement, then the new one's, once it has its table.
    assert statement.close.call_count == 2


def test_a_failing_statement_is_closed_and_the_error_raised() -> None:
    connection, statement = _native()
    statement.execute.side_effect = RuntimeError("no such table")
    cursor = Cursor(connection)

    with pytest.raises(RuntimeError, match="no such table"):
        cursor.execute_stream("SELECT n FROM missing")

    statement.close.assert_called_once()
    cursor.close()
    statement.close.assert_called_once()


def test_parameters_are_bound() -> None:
    connection, statement = _native()

    Cursor(connection).execute_stream("SELECT n FROM big WHERE n > ?", [1])

    statement.execute.assert_called_once_with("SELECT n FROM big WHERE n > ?", [1])
