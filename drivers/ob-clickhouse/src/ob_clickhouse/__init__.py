"""ob-clickhouse — OrionBelt Semantic Layer driver for ClickHouse (PEP 249 DB-API 2.0).

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Usage::

    import ob_clickhouse

    conn = ob_clickhouse.connect(host="localhost", database="default")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any

import clickhouse_connect

from ob_clickhouse.connection import Connection
from ob_clickhouse.exceptions import (
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
paramstyle = "pyformat"  # clickhouse-connect uses %(name)s placeholders


def connect(
    *,
    host: str = "localhost",
    port: int = 8123,
    username: str = "default",
    password: str = "",
    database: str = "default",
    secure: bool = False,
    # Extra clickhouse-connect kwargs
    settings: dict[str, Any] | None = None,
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a ClickHouse connection with OBML support.

    Parameters
    ----------
    host : str
        ClickHouse host (default: ``localhost``).
    port : int
        ClickHouse HTTP port (default: ``8123``).
    username : str
        Username (default: ``default``).
    password : str
        Password (default: empty string).
    database : str
        Database name (default: ``default``).
    secure : bool
        Use HTTPS (default: ``False``).
    settings : dict, optional
        Extra ClickHouse server settings.
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    client_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "database": database,
        "secure": secure,
    }
    if settings:
        client_kwargs["settings"] = settings

    client = clickhouse_connect.get_client(**client_kwargs)
    return Connection(
        client,
        ob_api_url=ob_api_url,
        ob_timeout=ob_timeout,
    )


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
