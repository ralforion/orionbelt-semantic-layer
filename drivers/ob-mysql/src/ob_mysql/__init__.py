"""ob-mysql — OrionBelt Semantic Layer driver for MySQL 8.0+ (PEP 249 DB-API 2.0).

Uses ``mysql-connector-python`` for database access.  OBML queries are compiled
to MySQL SQL via the OrionBelt REST API running in single-model mode.

``fetch_arrow_table()`` is emulated by fetching rows and converting to a
PyArrow Table (no native Arrow support in mysql-connector-python).

Usage::

    import ob_mysql

    conn = ob_mysql.connect(database="mydb", user="me", password="secret")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any

import mysql.connector  # type: ignore[import-untyped]

from ob_mysql.connection import Connection
from ob_mysql.exceptions import (
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
paramstyle = "format"  # mysql-connector-python uses %s placeholders


def connect(
    dsn: str | None = None,
    *,
    host: str = "localhost",
    port: int = 3306,
    database: str = "mysql",
    user: str | None = None,
    password: str | None = None,
    ssl_ca: str | None = None,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
    charset: str = "utf8mb4",
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a MySQL connection with OBML support.

    Parameters
    ----------
    dsn : str, optional
        Not used for MySQL — provided for API consistency with other drivers.
    host : str
        MySQL host (default: ``localhost``).
    port : int
        MySQL port (default: ``3306``).
    database : str
        Database name (default: ``mysql``).
    user : str, optional
        Username.
    password : str, optional
        Password.
    ssl_ca : str, optional
        Path to CA certificate file.
    ssl_cert : str, optional
        Path to client certificate file.
    ssl_key : str, optional
        Path to client key file.
    charset : str
        Character set (default: ``utf8mb4`` for full Unicode support).
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "database": database,
        "charset": charset,
    }
    if user is not None:
        kwargs["user"] = user
    if password is not None:
        kwargs["password"] = password

    # SSL configuration
    if ssl_ca or ssl_cert or ssl_key:
        ssl_config: dict[str, str] = {}
        if ssl_ca:
            ssl_config["ca"] = ssl_ca
        if ssl_cert:
            ssl_config["cert"] = ssl_cert
        if ssl_key:
            ssl_config["key"] = ssl_key
        kwargs["ssl_ca"] = ssl_ca
        kwargs["ssl_cert"] = ssl_cert
        kwargs["ssl_key"] = ssl_key

    native = mysql.connector.connect(**kwargs)
    return Connection(
        native,
        ob_api_url=ob_api_url,
        ob_timeout=ob_timeout,
    )


__all__ = [
    "apilevel",
    "threadsafety",
    "paramstyle",
    "connect",
    "Connection",
    "Warning",
    "Error",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
]
