"""PEP 249 Cursor with OBML interception for Databricks."""

from __future__ import annotations

from typing import Any

from ob_databricks.compiler import compile_obml, is_obml, parse_obml
from ob_databricks.exceptions import NotSupportedError, ProgrammingError
from ob_databricks.type_codes import DBR_TYPE_MAP, STRING


class Cursor:
    """DB-API 2.0 cursor that intercepts OBML YAML queries.

    If the query is OBML, it is compiled to Databricks SQL via the OrionBelt
    REST API before execution. Plain SQL is passed through unchanged.
    """

    arraysize: int = 1

    def __init__(
        self,
        native: Any,  # noqa: ANN401
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
        """PEP 249 cursor description — 7-item tuples per column.

        Databricks SQL connector returns string type names in ``type_code``
        (index 1 of each description column). These are mapped to PEP 249
        type objects via :data:`DBR_TYPE_MAP`.
        """
        native_desc = self._native.description
        if native_desc is None:
            return None
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for col in native_desc:
            name = col[0]
            type_name = str(col[1]).lower() if col[1] is not None else ""
            type_code = DBR_TYPE_MAP.get(type_name, STRING)
            cols.append((name, type_code, None, None, None, None, None))
        return tuple(cols)

    @property
    def rowcount(self) -> int:
        return self._native.rowcount

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Cursor is closed.")

    def execute(self, operation: str, parameters: Any = None) -> Cursor:  # noqa: ANN401
        """Execute a query — OBML YAML or plain SQL."""
        self._check_open()
        sql = self._resolve_sql(operation)
        if parameters is not None:
            self._native.execute(sql, parameters)
        else:
            self._native.execute(sql)
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:  # noqa: ANN401
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
            dialect="databricks",
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

    def fetch_arrow_table(self) -> Any:  # noqa: ANN401
        """Fetch all remaining rows as a PyArrow Table.

        Databricks SQL connector returns Arrow natively via its internal
        Thrift/Arrow result format.  More memory-efficient than ``fetchall()``.
        """
        self._check_open()
        return self._native.fetchall_arrow()

    def close(self) -> None:
        """Close the cursor."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def setinputsizes(self, _sizes: Any) -> None:  # noqa: ANN401
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    @property
    def lastrowid(self) -> None:
        """Databricks does not expose lastrowid via connector cursor."""
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
