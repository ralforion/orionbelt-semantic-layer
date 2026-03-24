"""ob-postgres — OrionBelt Semantic Layer driver for PostgreSQL (PEP 249 DB-API 2.0).

Uses ``adbc-driver-postgresql`` for native Arrow support.  ADBC provides
zero-copy Arrow results directly from the PostgreSQL wire protocol, enabling
efficient Arrow Flight SQL streaming.

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Usage::

    import ob_postgres

    conn = ob_postgres.connect(dbname="mydb", user="me", password="secret")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus, urlencode

import adbc_driver_postgresql.dbapi

from ob_postgres.connection import Connection
from ob_postgres.exceptions import (
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
paramstyle = "qmark"  # ADBC uses ? placeholders


def _build_pg_uri(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str | None,
    password: str | None,
    sslmode: str | None,
) -> str:
    """Build a PostgreSQL connection URI from keyword arguments."""
    userinfo = ""
    if user is not None:
        userinfo = quote_plus(user)
        if password is not None:
            userinfo += f":{quote_plus(password)}"
        userinfo += "@"
    params: dict[str, str] = {}
    if sslmode is not None:
        params["sslmode"] = sslmode
    query = f"?{urlencode(params)}" if params else ""
    return f"postgresql://{userinfo}{host}:{port}/{dbname}{query}"


def connect(
    dsn: str | None = None,
    *,
    host: str = "localhost",
    port: int = 5432,
    dbname: str = "postgres",
    user: str | None = None,
    password: str | None = None,
    sslmode: str | None = None,
    schema: str | None = None,
    options: dict[str, Any] | None = None,
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a PostgreSQL connection with OBML support (ADBC driver).

    Parameters
    ----------
    dsn : str, optional
        Full PostgreSQL URI (overrides individual params).
    host : str
        PostgreSQL host (default: ``localhost``).
    port : int
        PostgreSQL port (default: ``5432``).
    dbname : str
        Database name (default: ``postgres``).
    user : str, optional
        Username.
    password : str, optional
        Password.
    sslmode : str, optional
        SSL mode (``disable``, ``require``, ``verify-full``, etc.).
    schema : str, optional
        PostgreSQL schema — sets ``search_path`` after connecting.
    options : dict, optional
        Extra connection options (added as URI query parameters).
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    if dsn is not None:
        uri = dsn
    else:
        # Include search_path as a connection option if schema is specified
        extra_options = dict(options) if options else {}
        if schema:
            extra_options["options"] = f"-csearch_path={schema}"

        uri = _build_pg_uri(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
        )
        if extra_options:
            sep = "&" if "?" in uri else "?"
            uri += sep + urlencode(extra_options)

    native = adbc_driver_postgresql.dbapi.connect(uri)

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
