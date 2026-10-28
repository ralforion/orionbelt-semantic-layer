"""PEP 249 Cursor with OBML interception for MySQL.

A statement's result is read whole when it runs, through the connection's
client (see :class:`ob_mysql.connection.Connection`); the fetch methods page
through it. ``execute_stream()`` streams a result too large to buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ob_mysql.compiler import compile_obml, is_obml, parse_obml
from ob_mysql.exceptions import NotSupportedError, ProgrammingError

if TYPE_CHECKING:
    import pyarrow as pa

    from ob_mysql.connection import Connection, _Result


class Cursor:
    """DB-API 2.0 cursor that intercepts OBML YAML queries.

    If the query is OBML, it is compiled to MySQL SQL via the OrionBelt
    REST API before execution. Plain SQL is passed through unchanged.
    """

    arraysize: int = 1

    def __init__(
        self,
        connection: Connection,
        *,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._connection = connection
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout
        self._result: _Result | None = None
        self._position = 0
        # The native ADBC cursor a streamed result is read from, open until
        # the reader is done with it: closed by the next statement or close().
        self._streaming: Any = None

    @property
    def description(
        self,
    ) -> tuple[tuple[str, Any, None, None, None, None, None], ...] | None:
        """PEP 249 cursor description: 7-item tuples per column."""
        if self._result is None or not self._result.names:
            return None
        return tuple(
            (name, type_object, None, None, None, None, None)
            for name, type_object in zip(self._result.names, self._result.type_objects, strict=True)
        )

    @property
    def rowcount(self) -> int:
        return self._result.rowcount if self._result is not None else -1

    @property
    def lastrowid(self) -> int | None:
        """The last inserted row ID (PyMySQL only; ADBC does not report it)."""
        return self._result.lastrowid if self._result is not None else None

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Cursor is closed.")

    def _end_stream(self) -> None:
        """Close the native cursor of a streamed result, if one is open."""
        if self._streaming is not None:
            self._streaming.close()
            self._streaming = None

    def execute_stream(self, operation: str, parameters: Any = None) -> pa.RecordBatchReader:
        """Execute a query and stream its result as a ``pyarrow.RecordBatchReader``.

        Not PEP 249: :meth:`execute` reads the whole result, which a result too
        large for memory cannot afford. Over ADBC the reader reads it batch by
        batch as it is consumed, each batch in the schema :meth:`execute`
        gives, so its native cursor stays open until this cursor runs another
        statement or is closed. With parameters, or over PyMySQL, which reads
        a result whole, the result is read whole and handed over as a reader.
        ``description`` is set; ``rowcount`` is -1 when streamed, and the
        ``fetch*()`` methods return nothing.
        """
        self._check_open()
        self._end_stream()
        import pyarrow as pa

        from ob_mysql.arrow_types import type_object_for
        from ob_mysql.connection import _Result

        sql = self._resolve_sql(operation)
        if parameters is not None or self._connection.backend != "adbc":
            result = self._connection._execute(sql, parameters)
            table = result.arrow_from(0) if result.names else pa.table({})
            self._result = _Result(
                names=result.names,
                type_objects=result.type_objects,
                rowcount=result.rowcount,
                lastrowid=result.lastrowid,
                rows=[],
            )
            return pa.RecordBatchReader.from_batches(table.schema, table.to_batches())
        self._streaming, reader = self._connection._stream_adbc(sql)
        self._result = _Result(
            names=reader.schema.names,
            type_objects=[type_object_for(f.type) for f in reader.schema],
            rows=[],
        )
        self._position = 0
        return reader

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        """Execute a query: OBML YAML or plain SQL."""
        self._check_open()
        self._end_stream()
        sql = self._resolve_sql(operation)
        self._result = self._connection._execute(sql, parameters)
        self._position = 0
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        """Execute against all parameter sequences.

        OBML queries are not supported with executemany: raises NotSupportedError.
        """
        self._check_open()
        if is_obml(operation):
            raise NotSupportedError("executemany() is not supported for OBML queries.")
        for params in seq_of_parameters:
            self.execute(operation, params)

    def _resolve_sql(self, operation: str) -> str:
        """Compile OBML to SQL or return plain SQL unchanged."""
        if not is_obml(operation):
            return operation
        obml = parse_obml(operation)
        return compile_obml(
            obml,
            dialect="mysql",
            ob_api_url=self._ob_api_url,
            ob_timeout=self._ob_timeout,
        )

    def _rows(self) -> list[tuple[Any, ...]]:
        self._check_open()
        if self._result is None or not self._result.names:
            return []
        return self._result.all_rows()

    def fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row."""
        rows = self._rows()
        if self._position >= len(rows):
            return None
        self._position += 1
        return rows[self._position - 1]

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """Fetch the next *size* rows."""
        rows = self._rows()
        n = size if size is not None else self.arraysize
        chunk = rows[self._position : self._position + n]
        self._position += len(chunk)
        return chunk

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch all remaining rows."""
        rows = self._rows()
        chunk = rows[self._position :]
        self._position = len(rows)
        return chunk

    def fetch_arrow_table(self) -> Any:
        """Fetch all remaining rows as a PyArrow Table.

        The schema is the same through either client and for any number of
        rows: see :mod:`ob_mysql.arrow_types`.
        """
        self._check_open()
        if self._result is None or not self._result.names:
            raise ProgrammingError("No result set: execute a query first.")
        table = self._result.arrow_from(self._position)
        self._position += table.num_rows
        return table

    def close(self) -> None:
        """Close the cursor, and the native cursor a streamed result reads from."""
        self._end_stream()
        self._closed = True
        self._result = None

    def setinputsizes(self, _sizes: Any) -> None:
        """No-op: required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op: required by PEP 249."""

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> Cursor:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row
