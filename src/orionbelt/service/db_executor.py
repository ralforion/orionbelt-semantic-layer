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
import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


class ExecutionUnavailableError(Exception):
    """Raised when query execution is not available (missing package or config)."""


class ExecutionError(Exception):
    """Raised when database execution fails."""


class ColumnMeta:
    """Metadata for a single result column.

    ``default_format`` is the executor-suggested display pattern based on
    the column's Arrow / driver type. The API uses it as a fallback when
    the model has no explicit ``format`` for the column — typically raw-
    mode ``select.fields`` projections of physical columns.
    """

    __slots__ = ("name", "type_hint", "default_format", "scale", "precision")

    def __init__(
        self,
        name: str,
        type_hint: str,
        default_format: str | None = None,
        scale: int | None = None,
        precision: int | None = None,
    ) -> None:
        self.name = name
        self.type_hint = type_hint
        self.default_format = default_format
        # Populated for DECIMAL/NUMERIC columns so the pgwire surface can report
        # NUMERIC(precision, scale) instead of FLOAT8 (see issue #116).
        self.scale = scale
        self.precision = precision


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
        arrow_schema: Any = None,
    ) -> None:
        self.columns = columns
        self.row_count = row_count
        self.execution_time_ms = execution_time_ms
        self._arrow_table = arrow_table
        self._rows = raw_rows
        self._tz = tz
        # Captured up front, because ``rows`` drops the table to free memory.
        # Keeping the *schema* costs nothing and lets the response and cache
        # writers preserve declared types without having to read the table
        # before anything touches ``rows`` — an ordering hazard that would
        # otherwise be invisible at the call site.
        # ``arrow_schema`` lets a row-backed result still carry the types its
        # rows came from. A cache hit is rebuilt from ``raw_rows`` - because
        # ``table_to_rows`` keeps native dates where the Arrow row builder
        # serialises them - and without the schema the re-encode infers types
        # from values, so an empty or all-null ``int64`` column comes back
        # ``null``. That is the exact preservation the schema sidecar exists
        # for, and losing it on the way out would undo it.
        self._arrow_schema = getattr(arrow_table, "schema", None) or arrow_schema

    @property
    def timezone(self) -> str | None:
        """IANA timezone name used to label naive timestamps, or None."""
        return str(self._tz) if self._tz is not None else None

    @property
    def arrow_schema(self) -> Any | None:
        """The driver's Arrow schema, or ``None`` for a PEP 249 result.

        Survives ``rows`` materialisation. Encoders use it so an empty or
        all-null column keeps its declared type instead of decoding as
        ``null`` — otherwise a raw-arrow payload contradicts the column
        metadata in its own envelope.
        """
        return self._arrow_schema

    def reconcile_to_declared(self, declared: dict[str, Any]) -> list[tuple[str, str]]:
        """Cast the held Arrow table to the types the model declares. Idempotent.

        Must run before :attr:`rows`, which materialises and then frees the
        table. Returns one ``(column, reason)`` pair per column left alone, so
        the caller can report what it could not apply.

        A PEP 249 result has no table to cast and is skipped: there is nothing
        to reconcile *to* a declaration without Arrow types to compare, and the
        coarse hint the caller has instead cannot tell a boolean from a string.
        Since #412 that path is the exception rather than the default.
        """
        if self._arrow_table is None or not declared:
            return []
        from orionbelt.service.result_schema import reconcile_to_declared

        table, skipped = reconcile_to_declared(self._arrow_table, declared)
        self._arrow_table = table
        self._arrow_schema = table.schema
        by_name = {c.name: c for c in self.columns}
        for field in table.schema:
            column = by_name.get(field.name)
            if column is not None:
                column.type_hint = _arrow_type_to_hint(field.type)
        return skipped

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
        from ob_driver_core.type_codes import (
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
    # psycopg2 / psycopg3 expose ``cursor.description[i].type_code`` as a
    # Postgres OID integer. ``str(1700)`` is ``"1700"`` — never matches
    # the descriptive-name fallback below, so a column would default to
    # "string" and a downstream BI client would receive the value as TEXT
    # instead of a number. Recognise the well-known OIDs first.
    if isinstance(type_code, int):
        hint = _PG_OID_TO_HINT.get(type_code)
        if hint is not None:
            return hint
    # Fallback: inspect string representation of the type object (works for
    # mysql-connector, pyodbc, etc. which expose descriptive names)
    s = str(type_code).upper()
    if any(k in s for k in ("NUMBER", "NUMERIC", "DECIMAL", "INT", "FLOAT", "DOUBLE", "REAL")):
        return "number"
    if any(k in s for k in ("DATE", "TIME", "TIMESTAMP", "INTERVAL")):
        return "datetime"
    if any(k in s for k in ("BINARY", "BLOB", "BYTEA", "BYTES")):
        return "binary"
    return "string"


# Postgres type OIDs (pg_type.oid). Built from the official ``pg_type.h``
# entries the drivers ship with. Only the categories we route to the
# four coarse hints are listed; anything missing falls through to the
# descriptive-name fallback and ultimately to "string".
_PG_OID_TO_HINT: dict[int, str] = {
    # Integers
    20: "number",  # INT8
    21: "number",  # INT2
    23: "number",  # INT4
    26: "number",  # OID
    # Floating / exact numeric
    700: "number",  # FLOAT4
    701: "number",  # FLOAT8
    1700: "number",  # NUMERIC
    790: "number",  # MONEY
    # Booleans — surfaced as "string" historically; keep that.
    16: "string",  # BOOL
    # Text-ish
    18: "string",  # CHAR
    19: "string",  # NAME
    25: "string",  # TEXT
    1042: "string",  # BPCHAR
    1043: "string",  # VARCHAR
    2950: "string",  # UUID
    # Date / time
    1082: "datetime",  # DATE
    1083: "datetime",  # TIME
    1114: "datetime",  # TIMESTAMP
    1184: "datetime",  # TIMESTAMPTZ
    1186: "datetime",  # INTERVAL
    1266: "datetime",  # TIMETZ
    # Binary
    17: "binary",  # BYTEA
}


_DUCKDB_INT_PREFIXES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
)
_DUCKDB_FLOAT_DECIMAL_PREFIXES = ("FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")
_DUCKDB_NUMERIC_PREFIXES = _DUCKDB_INT_PREFIXES + _DUCKDB_FLOAT_DECIMAL_PREFIXES
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


def _default_format_for_duckdb_type(type_obj: Any) -> str | None:
    """Companion to ``_default_format_for_arrow_type`` for the DuckDB local
    execution path (PEP 249 fetchall). Same policy: ints stay unformatted,
    floats and decimals default to ``"#,##0.00"``.
    """
    s = str(type_obj).upper()
    if s.startswith(_DUCKDB_INT_PREFIXES):
        return None
    if s.startswith(_DUCKDB_FLOAT_DECIMAL_PREFIXES):
        return "#,##0.00"
    return None


_DECIMAL_TYPE_RE = re.compile(r"\b(?:decimal|numeric)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.IGNORECASE)


def parse_decimal_type(type_str: str) -> tuple[int, int] | None:
    """Return ``(precision, scale)`` for a ``DECIMAL(p, s)`` / ``NUMERIC(p, s)``
    type string, or ``None`` if it isn't a fixed-scale decimal.

    Used by the pgwire surface to report NUMERIC(precision, scale) instead of
    FLOAT8 for decimal measures/metrics (issue #116).
    """

    m = _DECIMAL_TYPE_RE.search(type_str or "")
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


def coarse_hint_from_type_name(name: str) -> str:
    """Map a SQL / Arrow type *name* string to a coarse hint.

    Single source of truth for the substring classification used by the Arrow
    OpaqueType fallback below and by the pgwire cache's column-type recovery
    (orionbelt.api.query_cache). Datetime / binary are matched *before* number
    so a temporal type whose name contains a numeric substring (e.g.
    ``interval`` contains ``int``) is not misclassified as a number.
    """
    lowered = (name or "").lower()
    # Before every other test: "bool" contains none of the numeric, temporal or
    # binary substrings, but keeping it first makes that independent of what is
    # added to those lists later.
    if "bool" in lowered:
        return "boolean"
    if any(t in lowered for t in ("timestamp", "date", "time", "interval")):
        return "datetime"
    if "bytea" in lowered or "binary" in lowered:
        return "binary"
    if any(
        t in lowered
        for t in ("int", "float", "double", "real", "decimal", "numeric", "money", "number")
    ):
        return "number"
    return "string"


def _arrow_type_to_hint(arrow_type: Any) -> str:
    """Map a PyArrow type to a simple type hint string.

    ADBC's PostgreSQL driver wraps types Arrow can't represent natively
    (NUMERIC, MONEY, INTERVAL, …) in ``OpaqueType``. ``pa.types.is_*``
    helpers all return False for OpaqueType, so we'd fall back to
    "string" — which is what caused Tableau measures to surface as TEXT
    on the pgwire surface and SUM to render as 0. Inspect the
    ``type_name`` attribute that OpaqueType carries to recover the
    underlying Postgres type name.
    """
    import pyarrow as pa

    # ``pa.types.is_*`` helpers call ``t.id`` internally — non-DataType
    # objects raise AttributeError. We catch that to keep the function
    # robust to OpaqueType subclasses, mocks, and other types that may
    # not implement the full PyArrow DataType protocol.
    try:
        # Before the numeric branch: Arrow has no "is_numeric" that excludes
        # bool, and a boolean folded into "string" is what made pgwire advertise
        # a declared boolean as TEXT.
        if pa.types.is_boolean(arrow_type):
            return "boolean"
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
            # DuckDB hands an INTERVAL back as month_day_nano_interval, and a
            # duration column as duration[unit]. Neither is a point in time,
            # but "datetime" is the bucket this codebase already puts
            # intervals in: ``_duckdb_type_hint`` classifies INTERVAL that
            # way, and ADBC's Postgres driver reaches the same answer via
            # ``coarse_hint_from_type_name("interval")`` (see
            # ``test_opaque_interval_maps_to_datetime``). Without these two
            # branches the same interval column would be "datetime" on the
            # PEP 249 path and "string" on the Arrow path, so a client would
            # see a different Postgres OID depending only on whether pyarrow
            # happened to be imported.
            or pa.types.is_interval(arrow_type)
            or pa.types.is_duration(arrow_type)
        ):
            return "datetime"
        if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
            return "binary"
    except (AttributeError, TypeError):
        pass
    type_name = getattr(arrow_type, "type_name", None)
    if isinstance(type_name, str):
        return coarse_hint_from_type_name(type_name)
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


def warm_db_tz_cache(dialect: str) -> ZoneInfo | None:
    """Probe the DB session timezone for ``dialect`` and cache the result.

    Idempotent: returns the cached value if detection has already run.
    Opens a short-lived connection (DuckDB read-only or pooled connection
    for other dialects) only when needed, so callers can pre-warm the
    cache from request handlers without waiting for the first query.

    Returns ``None`` (without raising) if the connection cannot be
    established or the detection query fails — leaves the cache in the
    same state ``_detect_db_timezone`` would on failure.
    """
    if dialect in _DB_TZ_DETECTED:
        return _DB_SESSION_TZ.get(dialect)

    try:
        if dialect == "duckdb":
            import duckdb
            from ob_flight.db_router import get_credentials

            creds = get_credentials("duckdb")
            database = creds.get("database", ":memory:")
            conn = duckdb.connect(database=database, read_only=True)
            try:
                return _detect_db_timezone(conn, "duckdb")
            finally:
                with contextlib.suppress(Exception):
                    conn.close()

        from ob_flight.db_router import get_connection

        with get_connection(dialect) as conn:
            cursor = conn.cursor()
            try:
                return _detect_db_timezone(cursor, dialect)
            finally:
                with contextlib.suppress(Exception):
                    cursor.close()
    except Exception:
        logger.debug("warm_db_tz_cache failed for %s", dialect, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Value serialisation (for non-Arrow fallback path)
# ---------------------------------------------------------------------------


def _serialize_value(val: Any, tz: ZoneInfo | None = None) -> Any:
    """Convert a Python value to a display-ready, mostly JSON-serializable type.

    For naive datetimes, applies the resolved timezone if available.
    Microseconds are elided when zero for cleaner output.

    ``Decimal`` is preserved as-is rather than cast to ``float``: a float
    only holds ~15-16 significant digits, so a wide NUMERIC (e.g.
    ``Decimal('123456789012345678.90')``) would round on the way through and
    surface as ``123456789012345680.00`` on the pgwire NUMERIC path (issue
    #136). Every boundary carries the Decimal without a lossy float hop — the
    pgwire encoder emits exact fixed-scale text, the Arrow result cache stores
    it as ``decimal128``/``decimal256``, and ``value_formatting.format_row``
    formats it directly.

    On the raw REST/JSON surface the value is deliberately delivered as an
    **exact decimal string** (Pydantic v2 serializes ``Decimal`` as a JSON
    string), not a lossy float — a fixed, value-independent contract so a
    column's JSON type never changes row to row. This is documented on
    ``api.schemas.ColumnMetadata.type``.
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
        return val
    if isinstance(val, bytes):
        return base64.b64encode(val).decode("ascii")
    return str(val)


def _serialize_row(row: Any, tz: ZoneInfo | None = None) -> list[Any]:
    """Convert a result row to a list of JSON-serializable values."""
    return [_serialize_value(v, tz) for v in row]


def _default_format_for_arrow_type(arrow_type: Any) -> str | None:
    """Suggest a display ``format`` pattern based on the column's Arrow type.

    Used as a fallback for columns that lack an explicit ``format`` on the
    model (raw-mode ``select.fields`` projections, measures that simply
    forgot to declare one). Integer columns deliberately get **no** default
    so IDs / keys render as plain digits (``"52965"``); floats and decimals
    get ``"#,##0.00"`` so monetary-style numbers come back locale-aware.

    For ADBC-style string-extension numerics the SQL ``type_name`` carried
    on the extension distinguishes int from decimal — int variants stay
    unformatted, others default to ``"#,##0.00"``.
    """
    try:
        import pyarrow as pa

        if pa.types.is_integer(arrow_type):
            return None  # plain str(val) — keeps IDs/keys clean
        if pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type):
            return "#,##0.00"
        if _is_string_stored_numeric_arrow_type(arrow_type):
            type_name = str(getattr(arrow_type, "type_name", "")).lower()
            return None if "int" in type_name else "#,##0.00"
        return None
    except Exception:  # noqa: BLE001 — never block row delivery on a type probe
        return None


def _is_string_stored_numeric_arrow_type(arrow_type: Any) -> bool:
    """Detect Arrow extension types that wrap a numeric SQL type as a string.

    ADBC's PostgreSQL driver (and similar high-precision-aware ADBC drivers)
    represents NUMERIC as ``arrow.opaque[storage_type=string, type_name=numeric]``
    to preserve precision beyond Arrow's ``decimal128`` limits. ``to_pydict()``
    then yields Python ``str`` rather than ``Decimal``. Without this detection
    every downstream consumer (UI, format_row, JSON, TSV, charts, runner) has
    to know to re-parse — easier to normalise once at the executor layer.
    """
    try:
        import pyarrow as pa

        # ADBC's ``OpaqueType`` (and other Arrow extension wrappers we care
        # about) carry both ``storage_type`` and ``type_name``. Plain Arrow
        # types (string, decimal128, etc.) carry neither — duck-type the
        # attribute pair rather than relying on a typecheck against
        # ``pa.ExtensionType`` which OpaqueType is not an instance of.
        storage = getattr(arrow_type, "storage_type", None)
        type_name = getattr(arrow_type, "type_name", None)
        if storage is None or type_name is None:
            return False
        if not pa.types.is_string(storage):
            return False
        if isinstance(type_name, bytes):
            type_name = type_name.decode("utf-8", "ignore")
        # Lazy import to avoid a circular dependency: db_executor is imported
        # by routers, which import schemas, which would otherwise pull this in.
        from orionbelt.service.value_formatting import is_numeric_type_hint

        return is_numeric_type_hint(str(type_name))
    except Exception:  # noqa: BLE001 — defensive: never block row delivery on a type probe
        return False


def _is_interval_arrow_type(arrow_type: Any) -> bool:
    """True for Arrow interval / duration types."""
    try:
        import pyarrow as pa

        return bool(pa.types.is_interval(arrow_type) or pa.types.is_duration(arrow_type))
    except (AttributeError, TypeError, ImportError):
        return False


def _interval_to_timedelta(val: Any) -> Any:
    """Normalise an Arrow interval cell to ``timedelta``, as DuckDB does.

    ``month_day_nano_interval`` comes out of ``to_pylist()`` as a
    ``MonthDayNano`` namedtuple, whose ``str()`` is
    ``"MonthDayNano(months=0, days=1, nanoseconds=0)"`` — while the PEP 249
    path returns a ``timedelta`` that serialises to ``"1 day, 0:00:00"``.
    The same query would then render differently depending only on whether
    pyarrow was imported.

    DuckDB's own PEP 249 conversion counts a month as 30 days (``INTERVAL 3
    MONTH`` -> ``timedelta(days=90)``, ``INTERVAL 1 YEAR`` -> 360 days), so
    reproducing that rule makes the two paths agree exactly. Arrow
    ``duration`` cells are already ``timedelta`` and pass through.
    """
    months = getattr(val, "months", None)
    if months is None:
        return val
    return timedelta(
        days=months * 30 + val.days,
        microseconds=val.nanoseconds // 1000,
    )


def _arrow_to_rows(table: Any, tz: ZoneInfo | None = None) -> list[list[Any]]:
    """Convert an Arrow Table to a list of JSON-serializable rows.

    String-stored numeric extension types are parsed to ``Decimal`` before
    the per-cell serialiser runs, so every downstream consumer sees the
    same shape regardless of driver. ``_serialize_value`` then handles the
    Decimal in its existing branch.
    """
    pydict = table.to_pydict()
    col_names = list(pydict.keys())
    n_rows = table.num_rows

    # Pre-compute which columns need string→Decimal parsing.
    string_numeric_cols: set[str] = {
        field.name for field in table.schema if _is_string_stored_numeric_arrow_type(field.type)
    }
    interval_cols: set[str] = {
        field.name for field in table.schema if _is_interval_arrow_type(field.type)
    }

    result: list[list[Any]] = []
    for i in range(n_rows):
        row: list[Any] = []
        for name in col_names:
            val = pydict[name][i]
            if name in string_numeric_cols and isinstance(val, str):
                with contextlib.suppress(TypeError, ValueError, InvalidOperation):
                    val = Decimal(val)
            elif name in interval_cols:
                val = _interval_to_timedelta(val)
            row.append(_serialize_value(val, tz))
        result.append(row)
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
        from ob_flight.db_router import get_credentials
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
    except _config_errors() as exc:
        # A credential / configuration problem, not a warehouse failure: surfacing it as
        # ExecutionError would render as a 502 and send the reader looking at
        # the database instead of at their environment.
        raise ExecutionUnavailableError(str(exc)) from None
    except Exception as exc:
        raise ExecutionError(f"Database execution failed: {exc}") from exc


def _duckdb_fetch_arrow(result: Any) -> Any:
    """Arrow fetch for a DuckDB result, or ``None``.

    DuckDB deprecated ``fetch_arrow_table()`` in favour of
    ``to_arrow_table()``; calling the old name emits a
    ``DeprecationWarning`` on every query. Prefer the new name and fall
    back to :func:`_try_fetch_arrow` for older ``duckdb`` releases
    (``pyproject`` allows ``>=1.0``), which also keeps the
    "pyarrow must already be imported" guard in one place.
    """
    import sys

    if "pyarrow" not in sys.modules:
        return None
    fetch_fn = getattr(result, "to_arrow_table", None)
    if fetch_fn is not None:
        try:
            return fetch_fn()
        except Exception:  # noqa: BLE001 — never fail a query on a fetch probe
            return None
    return _try_fetch_arrow(result)


def duckdb_execution_result(
    result: Any, t0: float, *, tz: ZoneInfo | None = None
) -> ExecutionResult:
    """Build an :class:`ExecutionResult` from an executed DuckDB result.

    Prefers an Arrow-native fetch, like :func:`_fetch_result` does for the
    pooled drivers. DuckDB already holds the result columnar, so
    ``fetchall()`` builds a Python object per cell only for
    ``ExecutionResult`` to hand most callers back to Arrow — and the
    round trip *re-infers* types, which collapses an empty or all-null
    ``int64`` column to ``null``. Carrying the table through preserves the
    exact schema and defers row materialisation to
    ``ExecutionResult.rows``. Falls back to the PEP 249 path when the
    Arrow fetch declines (pyarrow not loaded).

    Public (no leading underscore) so integration tests that must run
    against their own DuckDB connection can share this exact code rather
    than reimplementing it and silently drifting from production.
    """
    arrow_table = _duckdb_fetch_arrow(result)
    if arrow_table is not None:
        arrow_columns = [
            ColumnMeta(
                name=f.name,
                type_hint=_arrow_type_to_hint(f.type),
                default_format=_default_format_for_arrow_type(f.type),
            )
            for f in arrow_table.schema
        ]
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ExecutionResult(
            columns=arrow_columns,
            arrow_table=arrow_table,
            row_count=arrow_table.num_rows,
            execution_time_ms=round(elapsed_ms, 2),
            tz=tz,
        )

    rows_raw = result.fetchall()
    desc = result.description or []
    columns = [
        ColumnMeta(
            name=d[0],
            type_hint=_duckdb_type_hint(d[1]) if len(d) > 1 else "string",
            default_format=(_default_format_for_duckdb_type(d[1]) if len(d) > 1 else None),
        )
        for d in desc
    ]
    rows = [_serialize_row(r, tz) for r in rows_raw]
    elapsed_ms = (time.monotonic() - t0) * 1000
    return ExecutionResult(
        columns=columns,
        raw_rows=rows,
        row_count=len(rows),
        execution_time_ms=round(elapsed_ms, 2),
        tz=tz,
    )


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
    cross-process file lock conflicts with the notebook process. Result
    shaping lives in :func:`duckdb_execution_result`.
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
        return duckdb_execution_result(conn.execute(sql), t0, tz=effective_tz)
    finally:
        conn.close()


def _fetch_result(cursor: Any, t0: float, *, tz: ZoneInfo | None = None) -> ExecutionResult:
    """Fetch query results, preferring Arrow format when available."""
    # Try Arrow-native fetch first (DuckDB, Snowflake, etc.)
    arrow_table = _try_fetch_arrow(cursor)
    if arrow_table is not None:
        columns = [
            ColumnMeta(
                name=f.name,
                type_hint=_arrow_type_to_hint(f.type),
                default_format=_default_format_for_arrow_type(f.type),
            )
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


def explain_sql(sql: str, *, dialect: str) -> str:
    """Run ``EXPLAIN <sql>`` against the configured warehouse and return raw text.

    Returns the dialect-native EXPLAIN output as opaque text (newline-joined
    rows). OBSL does not normalize across dialects — callers should treat
    the output as a string in the named dialect's format.

    Raises:
        ExecutionUnavailableError: ob-flight or driver missing / not configured.
        ExecutionError: warehouse rejected the EXPLAIN.
    """
    try:
        from ob_flight.db_router import get_credentials
    except ImportError:
        raise ExecutionUnavailableError(
            "ob-flight-extension package is not installed. Install with: uv sync --extra flight"
        ) from None

    explain_stmt = f"EXPLAIN {sql}"
    try:
        if dialect == "duckdb":
            import duckdb

            creds = get_credentials(dialect)
            database = creds.get("database", ":memory:")
            conn = duckdb.connect(database=database, read_only=True)
            try:
                rows = conn.execute(explain_stmt).fetchall()
            finally:
                conn.close()
            return "\n".join("\t".join("" if c is None else str(c) for c in row) for row in rows)

        from ob_flight.db_router import get_connection

        with get_connection(dialect) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(explain_stmt)
                rows = cursor.fetchall() or []
            finally:
                with contextlib.suppress(Exception):
                    cursor.close()
        return "\n".join("\t".join("" if c is None else str(c) for c in row) for row in rows)
    except ExecutionUnavailableError:
        raise
    except KeyError as exc:
        raise ExecutionUnavailableError(str(exc)) from None
    except _config_errors() as exc:
        # Same contract as execute_sql: a credential / configuration problem
        # is not an EXPLAIN failure, and reporting it as one sends the reader
        # to the database instead of to their environment.
        raise ExecutionUnavailableError(str(exc)) from None
    except Exception as exc:
        raise ExecutionError(f"EXPLAIN failed: {exc}") from exc


_CONFIG_ERROR_TYPES: tuple[type[BaseException], ...] | None = None


def _config_errors() -> tuple[type[BaseException], ...]:
    """Exception types that mean "misconfigured", not "the warehouse failed".

    Resolved lazily and cached: ``ob_flight`` is an optional install, and the
    core package must not import it at module scope. Returns an empty tuple
    when it is absent, which makes the ``except`` clause a no-op.
    """
    global _CONFIG_ERROR_TYPES  # noqa: PLW0603
    if _CONFIG_ERROR_TYPES is None:
        try:
            from ob_flight.db_router import MotherDuckTokenMissingError

            _CONFIG_ERROR_TYPES = (MotherDuckTokenMissingError,)
        except Exception:  # noqa: BLE001 — ob_flight not installed
            _CONFIG_ERROR_TYPES = ()
    return _CONFIG_ERROR_TYPES


def ensure_arrow() -> bool:
    """Import pyarrow so the Arrow fetch paths are available. Idempotent.

    The two ``_try_fetch_*_arrow`` helpers refuse to fetch unless pyarrow is
    *already* in ``sys.modules``, and that guard is deliberate: importing it
    lazily inside a running uvicorn loop on macOS triggers heavy gRPC
    initialisation at the worst possible moment. What the guard does not do is
    ensure the import ever happens - so whether a result carries a driver
    schema depended on whatever else the process had touched first.

    That mattered more than it looks. ``ExecutionResult.arrow_schema`` is what
    ``cache.result_codec.build_result_table`` reads to type a column from the
    driver's declaration instead of from the values a result happens to
    contain (#410, and #393/#407 before it). With no schema it falls back to
    inference, and a ``decimal(18, 2)`` column comes back ``decimal128(3, 2)``
    for one filter and a different width for the next. The only thing importing
    pyarrow early was a cache warm-up gated on ``cache_backend == "file"``, and
    that setting defaults to ``noop`` - so on a default deployment the fix
    shipped in #410 never ran.

    Call this at a startup or entry boundary, never mid-request from inside a
    server's event loop; that is the hazard the guard exists for. Returns
    whether pyarrow is available, so a caller can log the degraded case rather
    than wonder about it.
    """
    with contextlib.suppress(ImportError):
        import pyarrow  # noqa: F401
        import pyarrow.ipc  # noqa: F401

        return True
    return False


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
