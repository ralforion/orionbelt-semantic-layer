"""Database query execution service.

Bridges the CompilationPipeline output to actual database execution
via ob-flight-extension's db_router. Used by POST /v1/query/execute.

Memory strategy: keeps result data in Arrow columnar format as long as
possible.  JSON-serializable rows are materialised lazily on first access
to ``ExecutionResult.rows``, avoiding an intermediate copy.
"""

from __future__ import annotations

import base64
import contextlib
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class ExecutionUnavailableError(Exception):
    """Raised when query execution is not available (missing package or config)."""


class ExecutionError(Exception):
    """Raised when database execution fails."""


class ColumnMeta:
    """Metadata for a single result column."""

    __slots__ = ("name", "type_hint")

    def __init__(self, name: str, type_hint: str) -> None:
        self.name = name
        self.type_hint = type_hint


class ExecutionResult:
    """Result of a database query execution.

    Holds data in Arrow format when available, deferring conversion to
    JSON-serializable Python rows until ``rows`` is first accessed.
    """

    def __init__(
        self,
        columns: list[ColumnMeta],
        *,
        arrow_table: Any = None,
        raw_rows: list[list[Any]] | None = None,
        row_count: int = 0,
        execution_time_ms: float = 0.0,
    ) -> None:
        self.columns = columns
        self.row_count = row_count
        self.execution_time_ms = execution_time_ms
        self._arrow_table = arrow_table
        self._rows = raw_rows

    @property
    def rows(self) -> list[list[Any]]:
        """JSON-serializable rows — materialised lazily from Arrow table."""
        if self._rows is None:
            if self._arrow_table is not None:
                self._rows = _arrow_to_rows(self._arrow_table)
                self._arrow_table = None  # free Arrow memory
            else:
                self._rows = []
        return self._rows


# ---------------------------------------------------------------------------
# Type mapping helpers
# ---------------------------------------------------------------------------


def _map_type_code(type_code: Any) -> str:
    """Map a PEP 249 type code to a simple string type hint."""
    try:
        from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, STRING

        if type_code == NUMBER or type_code is NUMBER:
            return "number"
        if type_code == STRING or type_code is STRING:
            return "string"
        if type_code == DATETIME or type_code is DATETIME:
            return "datetime"
        if type_code == BINARY or type_code is BINARY:
            return "binary"
    except ImportError:
        pass
    return "string"


def _arrow_type_to_hint(arrow_type: Any) -> str:
    """Map a PyArrow type to a simple type hint string."""
    import pyarrow as pa

    if (
        pa.types.is_integer(arrow_type)
        or pa.types.is_floating(arrow_type)
        or pa.types.is_decimal(arrow_type)
    ):
        return "number"
    if (
        pa.types.is_timestamp(arrow_type)
        or pa.types.is_date(arrow_type)
        or pa.types.is_time(arrow_type)
    ):
        return "datetime"
    if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return "binary"
    return "string"


# ---------------------------------------------------------------------------
# Value serialisation (for non-Arrow fallback path)
# ---------------------------------------------------------------------------


def _serialize_value(val: Any) -> Any:
    """Convert a Python value to a JSON-serializable type."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bytes):
        return base64.b64encode(val).decode("ascii")
    return str(val)


def _serialize_row(row: Any) -> list[Any]:
    """Convert a result row to a list of JSON-serializable values."""
    return [_serialize_value(v) for v in row]


def _arrow_to_rows(table: Any) -> list[list[Any]]:
    """Convert an Arrow Table to a list of JSON-serializable rows."""
    pydict = table.to_pydict()
    col_names = list(pydict.keys())
    n_rows = table.num_rows
    result: list[list[Any]] = []
    for i in range(n_rows):
        result.append([_serialize_value(pydict[name][i]) for name in col_names])
    return result


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_sql(sql: str, *, dialect: str) -> ExecutionResult:
    """Execute SQL against the configured vendor database.

    Uses connection pooling (via ``get_connection``) and tries Arrow-native
    fetch when the cursor supports it (e.g. DuckDB ``fetch_arrow_table``).

    The SQL is expected to include a LIMIT clause (enforced by the caller).

    Raises:
        ExecutionUnavailableError: if ob-flight-extension or vendor driver
            is not installed, or credentials are missing.
        ExecutionError: if the database connection or query fails.
    """
    try:
        from ob_flight.db_router import get_credentials
    except ImportError:
        raise ExecutionUnavailableError(
            "ob-flight-extension package is not installed. Install with: uv sync --extra flight"
        ) from None

    t0 = time.monotonic()
    try:
        if dialect == "duckdb":
            return _execute_duckdb(sql, get_credentials(dialect), t0)
        # Non-DuckDB: use the full ob driver via db_router
        from ob_flight.db_router import get_connection

        with get_connection(dialect) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                return _fetch_result(cursor, t0)
            finally:
                with contextlib.suppress(Exception):
                    cursor.close()
    except ExecutionUnavailableError:
        raise
    except KeyError as exc:
        raise ExecutionUnavailableError(str(exc)) from None
    except Exception as exc:
        raise ExecutionError(f"Database execution failed: {exc}") from exc


def _execute_duckdb(sql: str, creds: dict[str, Any], t0: float) -> ExecutionResult:
    """Execute SQL directly via native duckdb.

    Uses the native ``duckdb`` package with ``read_only=True`` to avoid
    cross-process file lock conflicts with the notebook process.
    """
    import duckdb

    database = creds.get("database", ":memory:")
    conn = duckdb.connect(database=database, read_only=True)
    try:
        result = conn.execute(sql)
        rows_raw = result.fetchall()
        desc = result.description or []
        columns = [ColumnMeta(name=d[0], type_hint="string") for d in desc]
        rows = [_serialize_row(r) for r in rows_raw]
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ExecutionResult(
            columns=columns,
            raw_rows=rows,
            row_count=len(rows),
            execution_time_ms=round(elapsed_ms, 2),
        )
    finally:
        conn.close()


def _fetch_result(cursor: Any, t0: float) -> ExecutionResult:
    """Fetch query results, preferring Arrow format when available."""
    # Try Arrow-native fetch first (DuckDB, Snowflake, etc.)
    arrow_table = _try_fetch_arrow(cursor)
    if arrow_table is not None:
        columns = [
            ColumnMeta(name=f.name, type_hint=_arrow_type_to_hint(f.type))
            for f in arrow_table.schema
        ]
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ExecutionResult(
            columns=columns,
            arrow_table=arrow_table,
            row_count=arrow_table.num_rows,
            execution_time_ms=round(elapsed_ms, 2),
        )

    # Fallback: PEP 249 fetchall()
    columns: list[ColumnMeta] = []
    if cursor.description:
        for col_desc in cursor.description:
            name = col_desc[0]
            type_code = col_desc[1]
            columns.append(ColumnMeta(name=name, type_hint=_map_type_code(type_code)))

    raw_rows = cursor.fetchall()
    rows = [_serialize_row(r) for r in raw_rows]
    elapsed_ms = (time.monotonic() - t0) * 1000

    return ExecutionResult(
        columns=columns,
        raw_rows=rows,
        row_count=len(rows),
        execution_time_ms=round(elapsed_ms, 2),
    )


def _try_fetch_arrow(cursor: Any) -> Any:
    """Try to fetch results as an Arrow Table. Returns None on failure.

    Disabled when pyarrow is not loaded to avoid triggering heavy gRPC
    initialization inside uvicorn on macOS.
    """
    import sys

    if "pyarrow" not in sys.modules:
        return None
    fetch_fn = getattr(cursor, "fetch_arrow_table", None)
    if fetch_fn is None:
        return None
    try:
        return fetch_fn()
    except Exception:
        return None
