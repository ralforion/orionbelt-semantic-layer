"""PEP 249 Cursor with OBML interception for DuckDB."""

from __future__ import annotations

from typing import Any

import duckdb

from ob_duckdb.compiler import compile_obml, is_obml, parse_obml
from ob_duckdb.exceptions import NotSupportedError, ProgrammingError
from ob_duckdb.type_codes import DUCKDB_TYPE_MAP, STRING


class Cursor:
    """DB-API 2.0 cursor that intercepts OBML YAML queries.

    If the query is OBML, it is compiled to DuckDB SQL via the OrionBelt
    REST API before execution. Plain SQL is passed through unchanged.
    """

    arraysize: int = 1

    def __init__(
        self,
        native: duckdb.DuckDBPyConnection,
        *,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._native = native
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout
        self._description: tuple[tuple[str, Any, None, None, None, None, None], ...] | None = None
        self._rowcount: int = -1

    @property
    def description(
        self,
    ) -> tuple[tuple[str, Any, None, None, None, None, None], ...] | None:
        """PEP 249 cursor description — 7-item tuples per column."""
        return self._description

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Cursor is closed.")

    def _build_description(self) -> None:
        """Build PEP 249 description from native cursor."""
        native_desc = self._native.description
        if native_desc is None:
            self._description = None
            return
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for col in native_desc:
            name = col[0]
            raw_type = col[1] if len(col) > 1 else None
            type_code = STRING  # default
            # DuckDB returns DuckDBPyType objects — convert to string for lookup
            type_str = str(raw_type).upper() if raw_type is not None else ""
            type_code = DUCKDB_TYPE_MAP.get(type_str, STRING)
            cols.append((name, type_code, None, None, None, None, None))
        self._description = tuple(cols)

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        """Execute a query — OBML YAML or plain SQL."""
        self._check_open()
        sql = self._resolve_sql(operation)
        if parameters is not None:
            self._native.execute(sql, parameters)
        else:
            self._native.execute(sql)
        self._build_description()
        self._rowcount = -1
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        """Execute against all parameter sequences.

        OBML queries are not supported with executemany — raises NotSupportedError.
        """
        self._check_open()
        if is_obml(operation):
            raise NotSupportedError("executemany() is not supported for OBML queries.")
        self._native.executemany(operation, seq_of_parameters)
        self._build_description()

    def _resolve_sql(self, operation: str) -> str:
        """Compile OBML to SQL or return plain SQL unchanged."""
        if not is_obml(operation):
            return operation
        obml = parse_obml(operation)
        return compile_obml(
            obml,
            dialect="duckdb",
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
        """Fetch all remaining rows as a PyArrow Table (zero-copy from DuckDB).

        Returns a ``pyarrow.Table``.  This avoids materialising intermediate
        Python row objects and is significantly more memory-efficient for
        large result sets.
        """
        self._check_open()
        return self._native.fetch_arrow_table()

    def close(self) -> None:
        """Close the cursor."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def setinputsizes(self, _sizes: Any) -> None:
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    @property
    def lastrowid(self) -> None:
        """DuckDB does not support lastrowid."""
        return None

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
