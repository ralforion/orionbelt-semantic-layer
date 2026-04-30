"""PEP 249 Cursor with OBML interception for BigQuery.

``execute()`` fetches results as an Arrow table via ``RowIterator.to_arrow()``
for zero-copy Arrow Flight support.  ``fetch_arrow_table()`` returns the
native Arrow table directly.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from ob_bigquery.compiler import compile_obml, is_obml, parse_obml
from ob_bigquery.exceptions import NotSupportedError, ProgrammingError
from ob_bigquery.type_codes import BQ_TYPE_MAP, STRING


class Cursor:
    """DB-API 2.0 cursor that intercepts OBML YAML queries.

    If the query is OBML, it is compiled to BigQuery SQL via the OrionBelt
    REST API before execution. Plain SQL is passed through unchanged.

    Results are kept in Arrow columnar format via ``to_arrow()``.
    ``fetch_arrow_table()`` returns the Arrow table directly for efficient
    Arrow Flight SQL streaming.
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
        self._results: Any = None
        self._rows: list[tuple[Any, ...]] = []
        self._index: int = 0
        self._arrow_table: pa.Table | None = None

    @property
    def description(
        self,
    ) -> tuple[tuple[str, Any, None, None, None, None, None], ...] | None:
        """PEP 249 cursor description — 7-item tuples per column."""
        if self._results is None:
            return None
        schema = self._results.schema
        if not schema:
            return None
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for field in schema:
            type_code = BQ_TYPE_MAP.get(field.field_type, STRING)
            cols.append((field.name, type_code, None, None, None, None, None))
        return tuple(cols)

    @property
    def rowcount(self) -> int:
        if self._results is None:
            return -1
        return self._results.total_rows or -1

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Cursor is closed.")

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        """Execute a query — OBML YAML or plain SQL.

        Fetches results as an Arrow table via ``to_arrow()`` for zero-copy
        Arrow Flight support.
        """
        self._check_open()
        sql = self._resolve_sql(operation)
        if parameters is not None:
            query_params = [
                _to_query_parameter(k, v) for k, v in parameters.items()
            ]
            from google.cloud.bigquery import QueryJobConfig

            job_config = QueryJobConfig(query_parameters=query_params)
            self._results = self._native.query(sql, job_config=job_config)
        else:
            self._results = self._native.query(sql)

        self._arrow_table = self._results.to_arrow()
        pydict = self._arrow_table.to_pydict()
        col_names = list(pydict.keys())
        n = self._arrow_table.num_rows
        self._rows = [tuple(pydict[c][i] for c in col_names) for i in range(n)]
        self._index = 0
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        """Execute against all parameter sequences.

        OBML queries are not supported with executemany — raises NotSupportedError.
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
            dialect="bigquery",
            ob_api_url=self._ob_api_url,
            ob_timeout=self._ob_timeout,
        )

    def fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row."""
        self._check_open()
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """Fetch the next *size* rows."""
        self._check_open()
        n = size if size is not None else self.arraysize
        end = min(self._index + n, len(self._rows))
        rows = self._rows[self._index : end]
        self._index = end
        return rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch all remaining rows."""
        self._check_open()
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def fetch_arrow_table(self) -> pa.Table:
        """Fetch all remaining rows as a PyArrow Table.

        BigQuery results are fetched via ``RowIterator.to_arrow()`` during
        ``execute()``, providing zero-copy Arrow access.  Significantly more
        memory-efficient for large result sets and enables efficient Arrow
        Flight SQL streaming.
        """
        self._check_open()
        if self._arrow_table is None:
            raise ProgrammingError("No Arrow result available — already consumed.")
        table = self._arrow_table
        self._arrow_table = None
        return table

    def close(self) -> None:
        """Close the cursor."""
        if not self._closed:
            self._results = None
            self._rows = []
            self._arrow_table = None
            self._closed = True

    def setinputsizes(self, _sizes: Any) -> None:
        """No-op — required by PEP 249."""

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        """No-op — required by PEP 249."""

    @property
    def lastrowid(self) -> None:
        """BigQuery does not support lastrowid."""
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


def _to_query_parameter(name: str, value: Any) -> Any:
    """Convert a Python value to a BigQuery ScalarQueryParameter."""
    from google.cloud.bigquery import ScalarQueryParameter

    if isinstance(value, int):
        return ScalarQueryParameter(name, "INT64", value)
    if isinstance(value, float):
        return ScalarQueryParameter(name, "FLOAT64", value)
    if isinstance(value, bool):
        return ScalarQueryParameter(name, "BOOL", value)
    return ScalarQueryParameter(name, "STRING", str(value))
