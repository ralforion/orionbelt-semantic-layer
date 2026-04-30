"""PEP 249 Cursor wrapping a clickhouse-connect Client for ClickHouse.

clickhouse-connect is **not** DB-API 2.0 — it exposes a ``Client`` with
``.query()`` / ``.command()`` methods and a ``QueryResult`` object.  This class
adapts that interface to PEP 249 so that OBML-aware code (and plain SQL) can
use standard cursor semantics.

Each ``execute()`` uses ``query_arrow()`` to keep data in Arrow columnar
format.  ``fetch_arrow_table()`` returns the Arrow table directly for
zero-copy Flight SQL streaming.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from ob_clickhouse.compiler import compile_obml, is_obml, parse_obml
from ob_clickhouse.exceptions import NotSupportedError, ProgrammingError
from ob_clickhouse.type_codes import BINARY, DATETIME, NUMBER, STRING


class Cursor:
    """DB-API 2.0 cursor wrapping a clickhouse-connect Client.

    ``execute()`` uses ``Client.query_arrow()`` to keep data in Arrow columnar
    format.  ``fetch_arrow_table()`` returns the Arrow table directly for
    zero-copy Flight SQL streaming.  ``fetchall()`` and ``fetchone()`` derive
    Python rows from the Arrow table.
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
        self._rows: list[tuple[Any, ...]] = []
        self._pos: int = 0
        self._description: (
            tuple[tuple[str, Any, None, None, None, None, None], ...] | None
        ) = None
        self._rowcount: int = -1
        self._arrow_table: Any = None

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
        """ClickHouse does not expose lastrowid."""
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
            dialect="clickhouse",
            ob_api_url=self._ob_api_url,
            ob_timeout=self._ob_timeout,
        )

    def _build_description_from_arrow(self, table: pa.Table) -> None:
        """Build PEP 249 description from a PyArrow Table schema."""
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for field in table.schema:
            if (
                pa.types.is_integer(field.type)
                or pa.types.is_floating(field.type)
                or pa.types.is_decimal(field.type)
            ):
                type_code = NUMBER
            elif (
                pa.types.is_timestamp(field.type)
                or pa.types.is_date(field.type)
                or pa.types.is_time(field.type)
            ):
                type_code = DATETIME
            elif pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type):
                type_code = BINARY
            else:
                type_code = STRING
            cols.append((field.name, type_code, None, None, None, None, None))
        self._description = tuple(cols) if cols else None

    @staticmethod
    def _arrow_to_rows(table: pa.Table) -> list[tuple[Any, ...]]:
        """Derive Python row tuples from an Arrow table."""
        pydict = table.to_pydict()
        col_names = list(pydict.keys())
        return [tuple(pydict[c][i] for c in col_names) for i in range(table.num_rows)]

    # -- PEP 249 execute methods ----------------------------------------------

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        """Execute a query — OBML YAML or plain SQL.

        Uses ``query_arrow()`` to keep data in Arrow columnar format.
        """
        self._check_open()
        sql = self._resolve_sql(operation)
        if parameters is not None:
            table = self._client.query_arrow(sql, parameters=parameters)
        else:
            table = self._client.query_arrow(sql)
        self._arrow_table = table
        self._rows = self._arrow_to_rows(table)
        self._pos = 0
        self._rowcount = table.num_rows
        self._build_description_from_arrow(table)
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        """Execute against all parameter sequences.

        OBML queries are not supported with executemany — raises NotSupportedError.
        """
        self._check_open()
        if is_obml(operation):
            raise NotSupportedError("executemany() is not supported for OBML queries.")
        for params in seq_of_parameters:
            self._client.query(operation, parameters=params)
        self._description = None
        self._rows = []
        self._pos = 0
        self._rowcount = -1

    # -- PEP 249 fetch methods ------------------------------------------------

    def fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row."""
        self._check_open()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """Fetch the next *size* rows."""
        self._check_open()
        n = size if size is not None else self.arraysize
        rows = self._rows[self._pos : self._pos + n]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch all remaining rows."""
        self._check_open()
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    def fetch_arrow_table(self) -> pa.Table:
        """Fetch all remaining rows as a PyArrow Table.

        Returns the native Arrow table directly — zero-copy, no intermediate
        Python row objects.  Significantly more memory-efficient for large
        result sets and enables efficient Arrow Flight SQL streaming.
        """
        self._check_open()
        if self._arrow_table is None:
            raise ProgrammingError("No Arrow result available — already consumed.")
        table = self._arrow_table
        self._arrow_table = None
        return table

    # -- PEP 249 no-ops -------------------------------------------------------

    def setinputsizes(self, _sizes: Any) -> None:
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the cursor.

        Does **not** close the underlying Client — the Client is owned by the
        Connection and shared across cursors.
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
