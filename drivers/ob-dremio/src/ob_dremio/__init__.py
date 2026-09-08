"""ob-dremio — OrionBelt Semantic Layer driver for Dremio (PEP 249 DB-API 2.0).

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Dremio is reached over Arrow Flight SQL through ``adbc-driver-flightsql``.
Dremio speaks Flight SQL natively, so the generic driver *is* the Dremio
driver -- there is no vendor SDK in this path, and none to maintain.

Usage::

    import ob_dremio

    conn = ob_dremio.connect(host="localhost", username="user", password="pass")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any

import adbc_driver_flightsql.dbapi

from ob_dremio.connection import Connection
from ob_dremio.exceptions import (
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
paramstyle = "qmark"  # Dremio SQL uses ? placeholders


def connect(
    *,
    host: str = "localhost",
    port: int = 32010,
    username: str | None = None,
    password: str | None = None,
    tls: bool = False,
    db_kwargs: dict[str, str] | None = None,
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a Dremio connection over Arrow Flight SQL with OBML support.

    Parameters
    ----------
    host : str
        Dremio host (default: ``localhost``).
    port : int
        Arrow Flight port (default: ``32010``).
    username : str, optional
        Dremio username for authentication.
    password : str, optional
        Dremio password for authentication.
    tls : bool
        Use TLS for the Flight connection (default: ``False``).
    db_kwargs : dict, optional
        Extra ADBC database options, e.g.
        ``{"adbc.flight.sql.rpc.call_header.routing_queue": "…"}`` for
        Dremio's workload-management headers. Merged last, so a caller can
        override anything derived from the arguments above.
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.

    Notes
    -----
    Authentication is Flight SQL's ``AuthenticateBasicToken``: the driver
    exchanges the credentials for a bearer token once and attaches it to
    every later call. Doing that by hand is what this driver used to carry
    a ``FlightCallOptions`` field for.
    """
    scheme = "grpc+tls" if tls else "grpc"
    uri = f"{scheme}://{host}:{port}"

    options: dict[str, str] = {}
    if username is not None:
        options["username"] = username
    if password is not None:
        options["password"] = password
    if db_kwargs:
        options.update(db_kwargs)

    # ``autocommit=True`` states what is already true rather than enabling
    # anything: ADBC otherwise tries to *disable* autocommit on connect, and
    # Dremio -- which has no transactions -- refuses, so every connection
    # warned "conn will not be DB-API 2.0 compliant". Same reason
    # ``Connection.commit()`` and ``rollback()`` are no-ops.
    native: Any = adbc_driver_flightsql.dbapi.connect(uri, db_kwargs=options, autocommit=True)

    return Connection(
        native,
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
