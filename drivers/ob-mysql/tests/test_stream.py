"""``execute_stream()``: an ADBC result read batch by batch, in :meth:`execute`'s schema."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import adbc_driver_manager
import pyarrow as pa
import pytest

from ob_mysql.connection import Connection
from ob_mysql.exceptions import DataError

UTC = pa.timestamp("us", tz="UTC")
TABLE = pa.table(
    {
        "n": [1, 2, 3],
        "at": pa.array([0, 1_000_000, 2_000_000], type=UTC),
    }
)


def _adbc(batches: Iterator[pa.RecordBatch] | None = None, *, rows: bool = True) -> MagicMock:
    """An ADBC connection whose cursor streams *batches*, TABLE's by default."""
    native = MagicMock()
    cursor = native.cursor.return_value
    cursor.description = [(name, None) for name in TABLE.column_names] if rows else []
    cursor.fetch_record_batch.return_value = pa.RecordBatchReader.from_batches(
        TABLE.schema, batches if batches is not None else TABLE.to_batches(max_chunksize=1)
    )
    cursor.fetch_arrow_table.return_value = TABLE
    return native


def test_streams_each_batch_in_the_schema_execute_gives() -> None:
    native = _adbc()
    connection = Connection(native, backend="adbc")
    cursor = connection.cursor()

    reader = cursor.execute_stream("SELECT n, at FROM big")
    streamed = reader.read_all()

    statement = native.cursor.return_value
    statement.fetch_arrow_table.assert_not_called()
    # The UTC label the driver attaches is dropped, as execute() drops it.
    assert streamed.schema.field("at").type == pa.timestamp("us")
    assert streamed.num_rows == 3
    assert reader.schema == streamed.schema
    buffered = connection.cursor().execute("SELECT n, at FROM big").fetch_arrow_table()
    assert streamed.equals(buffered)


def test_the_native_cursor_stays_open_until_the_cursor_closes() -> None:
    native = _adbc()
    cursor = Connection(native, backend="adbc").cursor()
    cursor.execute_stream("SELECT n, at FROM big")
    statement = native.cursor.return_value

    statement.close.assert_not_called()
    assert cursor.description is not None
    assert [d[0] for d in cursor.description] == ["n", "at"]
    assert cursor.rowcount == -1
    assert cursor.fetchall() == []

    cursor.close()
    statement.close.assert_called_once()


def test_a_value_the_driver_cannot_read_fails_the_read_saying_why() -> None:
    error = adbc_driver_manager.OperationalError(
        "UNKNOWN: [mysql] failed to append value to column 0: hour out of range",
        status_code=adbc_driver_manager.AdbcStatusCode.UNKNOWN,
    )

    def batches() -> Iterator[pa.RecordBatch]:
        yield TABLE.to_batches(max_chunksize=1)[0]
        raise error

    reader = Connection(_adbc(batches()), backend="adbc").cursor().execute_stream("SELECT 1")

    with pytest.raises(DataError, match="backend='pymysql'") as raised:
        reader.read_all()
    assert raised.value.__cause__ is error


def test_a_statement_without_a_result_answers_an_empty_reader() -> None:
    native = _adbc(rows=False)
    cursor = Connection(native, backend="adbc").cursor()

    reader = cursor.execute_stream("SET @x = 1")

    assert reader.schema == pa.schema([])
    native.cursor.return_value.close.assert_called_once()
    cursor.close()
    native.cursor.return_value.close.assert_called_once()


def test_bound_parameters_read_whole_through_executes_binding() -> None:
    native = _adbc()
    cursor = Connection(native, backend="adbc").cursor()

    reader = cursor.execute_stream("SELECT n, at FROM big WHERE n > %s", (0,))

    assert reader.read_all().column("n").to_pylist() == [1, 2, 3]
    native.cursor.return_value.fetch_record_batch.assert_not_called()


def test_pymysql_reads_whole() -> None:
    """PyMySQL has no stream: its rows are handed over as a reader."""
    native = MagicMock()
    statement = native.cursor.return_value
    # A LONG column: PyMySQL's column-definition packet, as test_driver fakes it.
    field = SimpleNamespace(name="n", type_code=3, flags=0, charsetnr=255, length=11, scale=0)
    statement._result = SimpleNamespace(fields=[field])
    statement.description = [("n", 3)]
    statement.fetchall.return_value = [(1,), (2,)]
    statement.rowcount = 2
    cursor = Connection(native, backend="pymysql").cursor()

    reader = cursor.execute_stream("SELECT n FROM big")

    assert reader.read_all().column("n").to_pylist() == [1, 2]
    assert cursor.rowcount == 2
