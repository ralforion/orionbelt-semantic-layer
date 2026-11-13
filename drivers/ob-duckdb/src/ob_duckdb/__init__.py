"""ob-duckdb — OrionBelt Semantic Layer driver for DuckDB (PEP 249 DB-API 2.0).

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Usage::

    import ob_duckdb

    conn = ob_duckdb.connect(database=":memory:")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import duckdb

from ob_duckdb.connection import Connection
from ob_duckdb.exceptions import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
)

# PEP 249 module-level constants
apilevel = "2.0"
threadsafety = 1  # threads may share the module but not connections
paramstyle = "qmark"  # DuckDB uses ? placeholders


def connect(
    database: str = ":memory:",
    *,
    read_only: bool = False,
    config: dict[str, Any] | None = None,
    init_sql: str | None = None,
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a DuckDB connection with OBML support.

    Parameters
    ----------
    database : str
        Path to ``.duckdb`` file or ``":memory:"`` (default).
    read_only : bool
        Open in read-only mode.
    config : dict
        DuckDB configuration options (threads, memory_limit, etc.).
    init_sql : str
        Statements run on this connection and on every cursor of it, such as
        ``LOAD iceberg``, ``CREATE SECRET``, ``ATTACH`` or ``SET search_path``.
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    init = _statements(init_sql) if init_sql else None
    native = duckdb.connect(database=database, read_only=read_only, config=config or {})
    if init is not None:
        try:
            init(native)
        except Exception:
            native.close()
            raise
    return Connection(
        native,
        ob_api_url=ob_api_url,
        ob_timeout=ob_timeout,
        init=init,
    )


def _statements(sql: str) -> Callable[[duckdb.DuckDBPyConnection], None]:
    """A connection setup that runs ``sql``."""

    def init(con: duckdb.DuckDBPyConnection) -> None:
        con.execute(sql)

    return init


__all__ = [
    "Connection",
    "DataError",
    "DatabaseError",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "Warning",
    "apilevel",
    "connect",
    "paramstyle",
    "threadsafety",
]
