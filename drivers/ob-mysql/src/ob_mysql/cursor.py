"""PEP 249 Cursor with OBML interception for MySQL.

Uses ``mysql-connector-python`` for database access.  Unlike the ADBC-based
PostgreSQL driver, ``fetch_arrow_table()`` is emulated by fetching all rows
via ``fetchall()`` and converting to a PyArrow Table using column metadata.
"""

from __future__ import annotations

from typing import Any

from ob_mysql.compiler import compile_obml, is_obml, parse_obml
from ob_mysql.exceptions import NotSupportedError, ProgrammingError
from ob_mysql.type_codes import MYSQL_TYPE_MAP, STRING


class Cursor:
    """DB-API 2.0 cursor that intercepts OBML YAML queries.

    If the query is OBML, it is compiled to MySQL SQL via the OrionBelt
    REST API before execution. Plain SQL is passed through unchanged.

    ``fetch_arrow_table()`` converts row results to a PyArrow Table.
    """

    arraysize: int = 1

    def __init__(
        self,
        native: Any,
        *,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._native = native
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout

    @property
    def description(
        self,
    ) -> tuple[tuple[str, Any, None, None, None, None, None], ...] | None:
        """PEP 249 cursor description — 7-item tuples per column."""
        native_desc = self._native.description
        if native_desc is None:
            return None
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for col in native_desc:
            name = col[0]
            type_code = col[1]
            mapped = MYSQL_TYPE_MAP.get(type_code, STRING) if isinstance(type_code, int) else STRING
            cols.append((name, mapped, None, None, None, None, None))
        return tuple(cols)

    @property
    def rowcount(self) -> int:
        return self._native.rowcount  # type: ignore[no-any-return]

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Cursor is closed.")

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        """Execute a query — OBML YAML or plain SQL."""
        self._check_open()
        sql = self._resolve_sql(operation)
        if parameters is not None:
            self._native.execute(sql, parameters)
        else:
            self._native.execute(sql)
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        """Execute against all parameter sequences.

        OBML queries are not supported with executemany — raises NotSupportedError.
        """
        self._check_open()
        if is_obml(operation):
            raise NotSupportedError("executemany() is not supported for OBML queries.")
        for params in seq_of_parameters:
            self._native.execute(operation, params)

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

    def fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row."""
        self._check_open()
        row = self._native.fetchone()
        return tuple(row) if row is not None else None

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """Fetch the next *size* rows."""
        self._check_open()
        n = size if size is not None else self.arraysize
        rows = self._native.fetchmany(n)
        return [tuple(r) for r in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch all remaining rows."""
        self._check_open()
        rows = self._native.fetchall()
        return [tuple(r) for r in rows]

    def fetch_arrow_table(self) -> Any:
        """Fetch all remaining rows as a PyArrow Table.

        mysql-connector-python has no native Arrow support, so this method
        fetches all rows via ``fetchall()`` and converts them to a PyArrow
        Table using column names from ``description``.
        """
        import pyarrow as pa

        self._check_open()
        rows = self._native.fetchall()
        desc = self._native.description or []
        col_names = [col[0] for col in desc]

        if not rows:
            # Return empty table with correct schema
            arrays = [pa.array([], type=pa.utf8()) for _ in col_names]
            return pa.table(dict(zip(col_names, arrays, strict=True)))

        # Transpose rows to columns and let PyArrow infer types
        columns: dict[str, list[Any]] = {name: [] for name in col_names}
        for row in rows:
            for i, name in enumerate(col_names):
                columns[name].append(row[i])

        return pa.table(columns)

    def close(self) -> None:
        """Close the cursor."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def setinputsizes(self, sizes: Any) -> None:
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    @property
    def lastrowid(self) -> int | None:
        """Return the last inserted row ID."""
        return self._native.lastrowid  # type: ignore[no-any-return]

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
