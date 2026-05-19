"""PEP 249 Cursor with OBML interception for PostgreSQL.

Uses ``adbc-driver-postgresql`` (Arrow Database Connectivity) for native
Arrow support.  ``fetch_arrow_table()`` returns a PyArrow Table directly
from the ADBC cursor — zero-copy columnar transfer for Arrow Flight SQL.
"""

from __future__ import annotations

from typing import Any

from ob_postgres.compiler import compile_obml, is_obml, parse_obml
from ob_postgres.exceptions import NotSupportedError, ProgrammingError
from ob_postgres.type_codes import BINARY, DATETIME, NUMBER, PG_OID_MAP, STRING


class Cursor:
    """DB-API 2.0 cursor that intercepts OBML YAML queries.

    If the query is OBML, it is compiled to PostgreSQL SQL via the OrionBelt
    REST API before execution. Plain SQL is passed through unchanged.

    Wraps an ``adbc_driver_postgresql.dbapi`` cursor which natively supports
    ``fetch_arrow_table()`` for zero-copy Arrow results.
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
        """PEP 249 cursor description — 7-item tuples per column.

        ``adbc-driver-postgresql`` returns PyArrow ``DataType`` objects
        (or ``OpaqueType`` for vendor-specific types like ``numeric``)
        as the ``type_code`` slot. psycopg2 returns OID integers. We
        handle both so downstream type-hint mapping (``_map_type_code``
        in db_executor.py) sees the right PEP 249 type constant.
        """
        native_desc = self._native.description
        if native_desc is None:
            return None
        cols: list[tuple[str, Any, None, None, None, None, None]] = []
        for col in native_desc:
            name = col[0]
            type_code = col[1]
            mapped = _classify_type_code(type_code)
            cols.append((name, mapped, None, None, None, None, None))
        return tuple(cols)

    @property
    def rowcount(self) -> int:
        return self._native.rowcount

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
            dialect="postgres",
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
        """Fetch all remaining rows as a PyArrow Table (ADBC native).

        ADBC provides zero-copy Arrow results directly from the PostgreSQL
        wire protocol.  Significantly more memory-efficient than materialising
        Python row objects and enables efficient Arrow Flight SQL streaming.
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
        """PostgreSQL does not expose lastrowid via ADBC cursor."""
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


# Substrings inside an OpaqueType's repr that identify the underlying
# Postgres type. ADBC wraps types it can't represent natively (NUMERIC,
# MONEY, JSON, …) in ``OpaqueType`` whose repr embeds ``type_name=<pg>``.
_OPAQUE_NUMBER_NAMES = ("numeric", "money", "decimal")
_OPAQUE_DATETIME_NAMES = ("timestamp", "date", "time", "interval")
_OPAQUE_BINARY_NAMES = ("bytea",)


def _classify_type_code(type_code: object) -> object:
    """Map a native ADBC type identifier to a PEP 249 type constant.

    ADBC's PostgreSQL driver populates ``cursor.description[i][1]``
    with PyArrow ``DataType`` objects — ``DataType(int32)``,
    ``TimestampType(timestamp[us, tz=UTC])``, ``DataType(string)`` —
    plus ``OpaqueType`` wrappers for types Arrow can't natively
    represent (NUMERIC ends up as
    ``OpaqueType(extension<arrow.opaque[storage_type=string,
    type_name=numeric, vendor_name=PostgreSQL]>)``).
    The legacy psycopg2 path supplies OID integers, kept for
    compatibility.
    """

    # psycopg2 / classic PEP 249 — bare OID.
    if isinstance(type_code, int):
        return PG_OID_MAP.get(type_code, STRING)

    # ADBC native PyArrow types — use pyarrow.types helpers when the
    # module is importable. ``pa.types.is_*`` calls ``t.id`` internally,
    # so an unrelated object (OpaqueType subclass, mock, etc.) trips
    # ``AttributeError`` — catch that and fall through to the
    # repr-substring branch below.
    try:
        import pyarrow as pa

        try:
            if pa.types.is_integer(type_code):
                return NUMBER
            if pa.types.is_floating(type_code) or pa.types.is_decimal(type_code):
                return NUMBER
            if pa.types.is_boolean(type_code):
                return STRING
            if (
                pa.types.is_timestamp(type_code)
                or pa.types.is_date(type_code)
                or pa.types.is_time(type_code)
                or pa.types.is_duration(type_code)
            ):
                return DATETIME
            if (
                pa.types.is_binary(type_code)
                or pa.types.is_large_binary(type_code)
                or pa.types.is_fixed_size_binary(type_code)
            ):
                return BINARY
            if pa.types.is_string(type_code) or pa.types.is_large_string(type_code):
                return STRING
        except (AttributeError, TypeError):
            pass
    except ImportError:
        pass

    # OpaqueType (NUMERIC, MONEY, JSON, …) — the type's repr embeds
    # ``type_name=<pg type>``. Match against the substring lists.
    s = str(type_code).lower()
    if any(name in s for name in _OPAQUE_NUMBER_NAMES):
        return NUMBER
    if any(name in s for name in _OPAQUE_DATETIME_NAMES):
        return DATETIME
    if any(name in s for name in _OPAQUE_BINARY_NAMES):
        return BINARY
    return STRING
