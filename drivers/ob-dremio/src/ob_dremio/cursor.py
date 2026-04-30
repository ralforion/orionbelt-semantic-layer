"""PEP 249 Cursor wrapping a pyarrow Flight client for Dremio.

Dremio exposes query execution via Arrow Flight.  This cursor wraps a
``pyarrow.flight.FlightClient`` and adapts it to PEP 249 semantics.

Each ``execute()`` fetches the entire result set into memory (client-side
buffering) by converting the Arrow table to Python tuples.
"""

from __future__ import annotations

from typing import Any

from ob_dremio.compiler import compile_obml, is_obml, parse_obml
from ob_dremio.exceptions import NotSupportedError, ProgrammingError
from ob_dremio.type_codes import ARROW_TYPE_MAP, STRING


class Cursor:
    """DB-API 2.0 cursor wrapping a pyarrow Flight client for Dremio.

    The underlying ``FlightClient.get_flight_info()`` + ``do_get()`` returns
    an Arrow record batch reader.  This cursor reads the full table, converts
    it to rows, and exposes them through the standard ``fetch*()`` methods.
    """

    arraysize: int = 1

    def __init__(
        self,
        client: Any,
        *,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._client = client
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout
        self._arrow_table: Any = None  # kept until fetch_arrow_table() or fetchall()
        self._rows: list[tuple[Any, ...]] = []
        self._pos: int = 0
        self._description: (
            tuple[tuple[str, Any, None, None, None, None, None], ...] | None
        ) = None
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

    def _execute_sql(self, sql: str) -> Any:
        """Execute SQL via Arrow Flight and return the result as an Arrow Table.

        Uses ``FlightDescriptor.for_command()`` + ``get_flight_info()`` +
        ``do_get()`` to retrieve results.
        """
        import pyarrow.flight

        descriptor = pyarrow.flight.FlightDescriptor.for_command(sql.encode("utf-8"))
        info = self._client.get_flight_info(descriptor)
        reader = self._client.do_get(info.endpoints[0].ticket)
        return reader.read_all()

    def _build_description(self, schema: Any) -> None:
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
    def _table_to_rows(table: Any) -> list[tuple[Any, ...]]:
        """Convert an Arrow Table to a list of tuples (row-major)."""
        columns = table.to_pydict()
        col_names = table.column_names
        n_rows = table.num_rows
        rows: list[tuple[Any, ...]] = []
        for i in range(n_rows):
            rows.append(tuple(columns[name][i] for name in col_names))
        return rows

    # -- PEP 249 execute methods ----------------------------------------------

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        """Execute a query — OBML YAML or plain SQL.

        Parameters are not supported for Dremio Flight queries.  If
        ``parameters`` is provided it is ignored (Dremio Flight SQL does
        not support parameterised queries via this interface).
        """
        self._check_open()
        sql = self._resolve_sql(operation)
        table = self._execute_sql(sql)
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

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        """Execute against all parameter sequences.

        OBML queries are not supported with executemany — raises NotSupportedError.
        """
        self._check_open()
        if is_obml(operation):
            raise NotSupportedError("executemany() is not supported for OBML queries.")
        for _params in seq_of_parameters:
            self._execute_sql(operation)
        self._description = None
        self._rows = []
        self._pos = 0
        self._rowcount = -1

    # -- PEP 249 fetch methods ------------------------------------------------

    def fetch_arrow_table(self) -> Any:
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

    def setinputsizes(self, _sizes: Any) -> None:
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the cursor.

        Does **not** close the underlying FlightClient — the client is owned
        by the Connection and shared across cursors.
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
