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
import logging
import time
from datetime import UTC, date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


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
        tz: ZoneInfo | None = None,
    ) -> None:
        self.columns = columns
        self.row_count = row_count
        self.execution_time_ms = execution_time_ms
        self._arrow_table = arrow_table
        self._rows = raw_rows
        self._tz = tz

    @property
    def timezone(self) -> str | None:
        """IANA timezone name used to label naive timestamps, or None."""
        return str(self._tz) if self._tz is not None else None

    @property
    def rows(self) -> list[list[Any]]:
        """JSON-serializable rows — materialised lazily from Arrow table."""
        if self._rows is None:
            if self._arrow_table is not None:
                self._rows = _arrow_to_rows(self._arrow_table, self._tz)
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
        from ob_driver_core.type_codes import (  # type: ignore[import-untyped]
            BINARY,
            DATETIME,
            NUMBER,
            STRING,
        )

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
    # Fallback: inspect string representation of the type object (works for
    # psycopg2, mysql-connector, pyodbc, etc. which expose descriptive names)
    s = str(type_code).upper()
    if any(k in s for k in ("NUMBER", "NUMERIC", "DECIMAL", "INT", "FLOAT", "DOUBLE", "REAL")):
        return "number"
    if any(k in s for k in ("DATE", "TIME", "TIMESTAMP", "INTERVAL")):
        return "datetime"
    if any(k in s for k in ("BINARY", "BLOB", "BYTEA", "BYTES")):
        return "binary"
    return "string"


_DUCKDB_NUMERIC_PREFIXES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "NUMERIC",
    "NUMBER",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
)
_DUCKDB_DATETIME_PREFIXES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")


def _duckdb_type_hint(type_obj: Any) -> str:
    """Map a DuckDB type descriptor to a simple type hint string."""
    s = str(type_obj).upper()
    if s.startswith(_DUCKDB_NUMERIC_PREFIXES):
        return "number"
    if s.startswith(_DUCKDB_DATETIME_PREFIXES):
        return "datetime"
    if s in ("BLOB",):
        return "binary"
    return "string"


def _arrow_type_to_hint(arrow_type: Any) -> str:
    """Map a PyArrow type to a simple type hint string."""
    import pyarrow as pa  # type: ignore[import-untyped]

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
# Timezone resolution
# ---------------------------------------------------------------------------

_HOST_TZ: ZoneInfo | None = None


def _get_host_timezone() -> ZoneInfo | None:
    """Resolve and cache the host process timezone (from TZ env or system)."""
    global _HOST_TZ  # noqa: PLW0603
    if _HOST_TZ is not None:
        return _HOST_TZ
    try:
        local_tz = datetime.now(UTC).astimezone().tzinfo
        if local_tz is not None:
            tz_name = str(local_tz)
            if tz_name and tz_name != "UTC":
                _HOST_TZ = ZoneInfo(tz_name)
                logger.info("Host timezone resolved: %s", tz_name)
                return _HOST_TZ
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        pass
    return None


def resolve_timezone(
    *,
    default_timezone: str | None = None,
) -> ZoneInfo:
    """Resolve the model-level fallback timezone for naive timestamp coercion.

    This is used as a fallback when the database session timezone cannot be
    detected at execution time.

    Resolution order (fallback chain):
    1. Model setting (default_timezone)
    2. Host process timezone (if not UTC)
    3. UTC (automatic final fallback)

    At execution time, the actual database session timezone takes priority
    over this fallback unless ``overrideDatabaseTimezone`` is set
    (see ``_detect_db_timezone``).
    """
    if default_timezone:
        try:
            return ZoneInfo(default_timezone)
        except (ZoneInfoNotFoundError, KeyError):
            pass

    host_tz = _get_host_timezone()
    if host_tz is not None:
        return host_tz

    return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Database session timezone detection
# ---------------------------------------------------------------------------

_DB_SESSION_TZ: dict[str, ZoneInfo] = {}
_DB_TZ_DETECTED: set[str] = set()

_TZ_QUERIES: dict[str, str | None] = {
    "snowflake": "SELECT CURRENT_TIMEZONE()",
    "postgres": "SELECT current_setting('TIMEZONE')",
    "mysql": "SELECT @@session.time_zone",
    "duckdb": "SELECT current_setting('TimeZone')",
    "bigquery": None,
    "clickhouse": "SELECT timezone()",
    "databricks": None,
    "dremio": None,
}


def _detect_db_timezone(executor: Any, dialect: str) -> ZoneInfo | None:
    """Detect and cache the database session timezone.

    Issues a one-time lightweight query per dialect. Subsequent calls return
    the cached result. Returns None if detection fails or is unsupported.

    For Arrow Flight: the detection happens on the same connection before
    fetching results, so Flight-served results get the correct TZ applied.
    """
    if dialect in _DB_TZ_DETECTED:
        return _DB_SESSION_TZ.get(dialect)

    _DB_TZ_DETECTED.add(dialect)

    if dialect == "bigquery":
        tz = ZoneInfo("UTC")
        _DB_SESSION_TZ[dialect] = tz
        logger.info("Database session timezone for %s: UTC (fixed)", dialect)
        return tz

    query = _TZ_QUERIES.get(dialect)
    if not query:
        return None

    try:
        result = executor.execute(query)
        fetcher = result if hasattr(result, "fetchone") else executor
        row = fetcher.fetchone()
        if row:
            tz_name = str(row[0]).strip()
            if tz_name and tz_name != "SYSTEM":
                zi = ZoneInfo(tz_name)
                _DB_SESSION_TZ[dialect] = zi
                logger.info("Database session timezone for %s: %s", dialect, tz_name)
                return zi
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        logger.debug("Invalid timezone name from %s", dialect)
    except Exception:
        logger.debug("Could not detect session timezone for %s", dialect, exc_info=True)

    return None


def _reset_db_tz_cache() -> None:
    """Reset the database timezone cache (for testing)."""
    _DB_SESSION_TZ.clear()
    _DB_TZ_DETECTED.clear()


# ---------------------------------------------------------------------------
# Value serialisation (for non-Arrow fallback path)
# ---------------------------------------------------------------------------


def _serialize_value(val: Any, tz: ZoneInfo | None = None) -> Any:
    """Convert a Python value to a JSON-serializable type.

    For naive datetimes, applies the resolved timezone if available.
    Microseconds are elided when zero for cleaner output.
    """
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, datetime):
        if val.tzinfo is None and tz is not None:
            val = val.replace(tzinfo=tz)
        s = val.isoformat()
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s
    if isinstance(val, dt_time):
        s = val.isoformat()
        if s.endswith(".000000"):
            s = s[:-7]
        return s
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bytes):
        return base64.b64encode(val).decode("ascii")
    return str(val)


def _serialize_row(row: Any, tz: ZoneInfo | None = None) -> list[Any]:
    """Convert a result row to a list of JSON-serializable values."""
    return [_serialize_value(v, tz) for v in row]


def _arrow_to_rows(table: Any, tz: ZoneInfo | None = None) -> list[list[Any]]:
    """Convert an Arrow Table to a list of JSON-serializable rows."""
    pydict = table.to_pydict()
    col_names = list(pydict.keys())
    n_rows = table.num_rows
    result: list[list[Any]] = []
    for i in range(n_rows):
        result.append([_serialize_value(pydict[name][i], tz) for name in col_names])
    return result


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_sql(
    sql: str,
    *,
    dialect: str,
    tz: ZoneInfo | None = None,
    override_db_tz: bool = False,
) -> ExecutionResult:
    """Execute SQL against the configured vendor database.

    Uses connection pooling (via ``get_connection``) and tries Arrow-native
    fetch when the cursor supports it (e.g. DuckDB ``fetch_arrow_table``).

    The SQL is expected to include a LIMIT clause (enforced by the caller).

    Args:
        sql: The compiled SQL to execute.
        dialect: Target database dialect name.
        tz: Resolved timezone for naive timestamp coercion in results.
        override_db_tz: If True, use ``tz`` instead of the detected
            database session timezone (for cases where naive timestamps
            are stored in a known timezone that differs from the DB session).

    Raises:
        ExecutionUnavailableError: if ob-flight-extension or vendor driver
            is not installed, or credentials are missing.
        ExecutionError: if the database connection or query fails.
    """
    try:
        from ob_flight.db_router import get_credentials  # type: ignore[import-untyped]
    except ImportError:
        raise ExecutionUnavailableError(
            "ob-flight-extension package is not installed. Install with: uv sync --extra flight"
        ) from None

    t0 = time.monotonic()
    try:
        if dialect == "duckdb":
            return _execute_duckdb(
                sql, get_credentials(dialect), t0, tz=tz, override_db_tz=override_db_tz
            )
        # Non-DuckDB: use the full ob driver via db_router
        from ob_flight.db_router import get_connection

        with get_connection(dialect) as conn:
            cursor = conn.cursor()
            try:
                effective_tz: ZoneInfo | None
                if override_db_tz and tz is not None:
                    effective_tz = tz
                else:
                    db_tz = _detect_db_timezone(cursor, dialect)
                    effective_tz = db_tz or tz
                cursor.execute(sql)
                return _fetch_result(cursor, t0, tz=effective_tz)
            finally:
                with contextlib.suppress(Exception):
                    cursor.close()
    except ExecutionUnavailableError:
        raise
    except KeyError as exc:
        raise ExecutionUnavailableError(str(exc)) from None
    except Exception as exc:
        raise ExecutionError(f"Database execution failed: {exc}") from exc


def _execute_duckdb(
    sql: str,
    creds: dict[str, Any],
    t0: float,
    *,
    tz: ZoneInfo | None = None,
    override_db_tz: bool = False,
) -> ExecutionResult:
    """Execute SQL directly via native duckdb.

    Uses the native ``duckdb`` package with ``read_only=True`` to avoid
    cross-process file lock conflicts with the notebook process.
    """
    import duckdb

    database = creds.get("database", ":memory:")
    conn = duckdb.connect(database=database, read_only=True)
    try:
        effective_tz: ZoneInfo | None
        if override_db_tz and tz is not None:
            effective_tz = tz
        else:
            db_tz = _detect_db_timezone(conn, "duckdb")
            effective_tz = db_tz or tz
        result = conn.execute(sql)
        rows_raw = result.fetchall()
        desc = result.description or []
        columns = [
            ColumnMeta(name=d[0], type_hint=_duckdb_type_hint(d[1]) if len(d) > 1 else "string")
            for d in desc
        ]
        rows = [_serialize_row(r, effective_tz) for r in rows_raw]
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ExecutionResult(
            columns=columns,
            raw_rows=rows,
            row_count=len(rows),
            execution_time_ms=round(elapsed_ms, 2),
            tz=effective_tz,
        )
    finally:
        conn.close()


def _fetch_result(cursor: Any, t0: float, *, tz: ZoneInfo | None = None) -> ExecutionResult:
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
            tz=tz,
        )

    # Fallback: PEP 249 fetchall()
    pep_columns: list[ColumnMeta] = []
    if cursor.description:
        for col_desc in cursor.description:
            name = col_desc[0]
            type_code = col_desc[1]
            pep_columns.append(ColumnMeta(name=name, type_hint=_map_type_code(type_code)))

    raw_rows = cursor.fetchall()
    rows = [_serialize_row(r, tz) for r in raw_rows]
    elapsed_ms = (time.monotonic() - t0) * 1000

    return ExecutionResult(
        columns=pep_columns,
        raw_rows=rows,
        row_count=len(rows),
        execution_time_ms=round(elapsed_ms, 2),
        tz=tz,
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
