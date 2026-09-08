"""PEP 249 Cursor executing over an ADBC Flight SQL connection to Dremio.

Dremio exposes query execution via Arrow Flight SQL, so the generic
``adbc-driver-flightsql`` driver is its driver.  This cursor adapts that
to PEP 249 semantics.

Each ``execute()`` fetches the entire result set into memory (client-side
buffering) and converts the Arrow table to Python tuples on first fetch.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ob_dremio.compiler import compile_obml, is_obml, parse_obml
from ob_dremio.exceptions import NotSupportedError, ProgrammingError
from ob_dremio.type_codes import ARROW_TYPE_MAP, STRING

if TYPE_CHECKING:
    import pyarrow as pa


class Cursor:
    """DB-API 2.0 cursor executing over an ADBC Flight SQL connection.

    Each statement runs on its own native cursor, so the result is Arrow and
    the table is kept here; ``description`` comes from its schema and rows
    are materialised lazily for the standard ``fetch*()`` methods.

    ``description`` is derived from the Arrow schema rather than from the
    native cursor's own ``description``, which reports PyArrow ``DataType``
    objects in the type-code slot where PEP 249 expects a type constant.
    """

    arraysize: int = 1

    def __init__(
        self,
        native_connection: Any,
        *,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._native_connection = native_connection
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout
        self._arrow_table: pa.Table | None = None  # kept until fetch_arrow_table()
        self._rows: list[tuple[Any, ...]] = []
        self._pos: int = 0
        self._description: tuple[tuple[str, Any, None, None, None, None, None], ...] | None = None
        self._rowcount: int = -1

    # -- PEP 249 attributes --------------------------------------------------

    @property
    def description(
        self,
    ) -> tuple[tuple[str, Any, None, None, None, None, None], ...] | None:
        """PEP 249 cursor description — 7-item tuples per column."""
        return self._description

    @property
    def rowcount(self) -> int:
        """Number of rows produced by the last ``execute()``."""
        return self._rowcount

    @property
    def lastrowid(self) -> None:
        """Dremio does not expose lastrowid."""
        return None

    # -- Internal helpers -----------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Cursor is closed.")

    def _resolve_sql(self, operation: str) -> str:
        """Compile OBML to SQL or return plain SQL unchanged."""
        if not is_obml(operation):
            return operation
        obml = parse_obml(operation)
        return compile_obml(
            obml,
            dialect="dremio",
            ob_api_url=self._ob_api_url,
            ob_timeout=self._ob_timeout,
        )

    def _execute_sql(self, sql: str, parameters: Sequence[object] | None = None) -> pa.Table:
        """Execute SQL over Flight SQL and return the result as an Arrow Table.

        Auth is the driver's problem now: ADBC exchanges the credentials for
        a bearer token during the handshake and attaches it to every call,
        which this driver used to carry ``FlightCallOptions`` to do by hand.

        One native cursor per statement, closed as soon as the table is in
        hand. Reusing it would be the natural thing to do, and against Dremio
        it is wrong: ADBC skips re-preparing when the SQL text has not
        changed, and Dremio then answers the second execution with the
        **first** execution's rows -- silently, with different parameters
        bound. The same reuse against OBSL's own Flight server rebinds
        correctly, which is what places the fault on Dremio's side of the
        wire. A fresh statement per execution is also exactly what the
        hand-rolled Flight client did, one ``get_flight_info`` + ``do_get``
        at a time.
        """
        native = self._native_connection.cursor()
        try:
            if parameters is not None:
                native.execute(sql, parameters)
            else:
                native.execute(sql)
            table: pa.Table = native.fetch_arrow_table()
            return table
        finally:
            native.close()

    def _build_description(self, schema: pa.Schema) -> None:
        """Build PEP 249 description from an Arrow schema."""
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for field in schema:
            type_str = str(field.type)
            # Strip parameters: "timestamp[ns]" -> "timestamp",
            # "decimal128(18, 2)" -> "decimal128"
            base_type = type_str.split("[")[0].split("(")[0]
            type_code = ARROW_TYPE_MAP.get(base_type, STRING)
            cols.append((field.name, type_code, None, None, None, None, None))
        self._description = tuple(cols) if cols else None

    @staticmethod
    def _table_to_rows(table: pa.Table) -> list[tuple[Any, ...]]:
        """Convert an Arrow Table to a list of tuples (row-major)."""
        columns = table.to_pydict()
        col_names = table.column_names
        n_rows = table.num_rows
        rows: list[tuple[Any, ...]] = []
        for i in range(n_rows):
            rows.append(tuple(columns[name][i] for name in col_names))
        return rows

    # -- PEP 249 execute methods ----------------------------------------------

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> Cursor:
        """Execute a query — OBML YAML or plain SQL.

        ``parameters`` bind as Flight SQL prepared-statement values.  The
        hand-rolled Flight client had nowhere to put them, so it passed the
        statement through with its placeholders intact and Dremio answered
        with a Calcite internal error about ``RexDynamicParam``.
        """
        self._check_open()
        sql = self._resolve_sql(operation)
        table = self._execute_sql(sql, parameters)
        self._arrow_table = table  # keep Arrow — converted lazily
        self._rows = []
        self._pos = 0
        self._rowcount = table.num_rows
        self._build_description(table.schema)
        return self

    def _ensure_rows(self) -> None:
        """Materialise Arrow table to rows on first fetch (lazy)."""
        if self._arrow_table is not None and not self._rows:
            self._rows = self._table_to_rows(self._arrow_table)
            self._arrow_table = None  # free Arrow memory

    def executemany(self, operation: str, seq_of_parameters: Sequence[Sequence[object]]) -> None:
        """Execute against all parameter sequences.

        Executed one statement per parameter set rather than through ADBC's
        ``executemany``, which binds a whole batch over ``DoPut`` — Dremio
        answers that with ``acceptPut is not implemented``.

        OBML queries are not supported with executemany — raises NotSupportedError.
        """
        self._check_open()
        if is_obml(operation):
            raise NotSupportedError("executemany() is not supported for OBML queries.")
        for params in seq_of_parameters:
            self._execute_sql(operation, params)
        self._description = None
        self._rows = []
        self._pos = 0
        self._rowcount = -1

    # -- PEP 249 fetch methods ------------------------------------------------

    def fetch_arrow_table(self) -> pa.Table | None:
        """Return the result as a PyArrow Table (zero-copy).

        Dremio uses Arrow Flight natively, so this avoids the overhead of
        converting to Python row tuples entirely.  After calling this method
        the Arrow table is consumed — subsequent ``fetchall()`` calls return
        an empty list.
        """
        self._check_open()
        table = self._arrow_table
        self._arrow_table = None
        return table

    def fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row."""
        self._check_open()
        self._ensure_rows()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """Fetch the next *size* rows."""
        self._check_open()
        self._ensure_rows()
        n = size if size is not None else self.arraysize
        rows = self._rows[self._pos : self._pos + n]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch all remaining rows."""
        self._check_open()
        self._ensure_rows()
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    # -- PEP 249 no-ops -------------------------------------------------------

    def setinputsizes(self, _sizes: Sequence[object]) -> None:
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the cursor.

        The native statement is already closed — ``_execute_sql`` opens one
        per execution and releases it as soon as the Arrow table is in hand.
        The connection stays open: it is owned by :class:`Connection` and
        shared across cursors.
        """
        if not self._closed:
            self._closed = True

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
