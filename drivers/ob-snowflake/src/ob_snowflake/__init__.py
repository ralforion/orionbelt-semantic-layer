"""ob-snowflake — OrionBelt Semantic Layer driver for Snowflake (PEP 249 DB-API 2.0).

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Usage::

    import ob_snowflake

    conn = ob_snowflake.connect(
        account="xy12345.eu-west-1",
        user="me",
        password="secret",
        database="MYDB",
        warehouse="COMPUTE_WH",
    )
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any

import snowflake.connector

from ob_snowflake.connection import Connection
from ob_snowflake.exceptions import (
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
paramstyle = "pyformat"  # snowflake-connector-python uses %(name)s and %s


def connect(
    account: str,
    *,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
    schema: str | None = None,
    warehouse: str | None = None,
    role: str | None = None,
    authenticator: str | None = None,
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a Snowflake connection with OBML support.

    Parameters
    ----------
    account : str
        Snowflake account identifier (e.g. ``xy12345.eu-west-1``).
    user : str, optional
        Username.
    password : str, optional
        Password.
    database : str, optional
        Default database.
    schema : str, optional
        Default schema.
    warehouse : str, optional
        Virtual warehouse.
    role : str, optional
        Snowflake role.
    authenticator : str, optional
        Authentication method (e.g. ``"externalbrowser"`` for SSO).
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    connect_kwargs: dict[str, Any] = {"account": account}
    if user is not None:
        connect_kwargs["user"] = user
    if password is not None:
        connect_kwargs["password"] = password
    if database is not None:
        connect_kwargs["database"] = database
    if schema is not None:
        connect_kwargs["schema"] = schema
    if warehouse is not None:
        connect_kwargs["warehouse"] = warehouse
    if role is not None:
        connect_kwargs["role"] = role
    if authenticator is not None:
        connect_kwargs["authenticator"] = authenticator

    # Without this, the connector converts every NUMBER with a scale to a
    # float on the Arrow path: ``CAST(2.55 AS NUMBER(18, 2))`` comes back as
    # ``double``, and ``NUMBER(19, 2)`` holding 12345678901234567.89 comes
    # back 11 cents wrong. Snowflake is the only vendor whose driver drops a
    # fixed-point type it was handed; every other ob-* driver returns
    # ``decimal128``. A semantic layer whose measures are money cannot make
    # that trade, so exactness is the default rather than an opt-in.
    connect_kwargs["arrow_number_to_decimal"] = True

    native = snowflake.connector.connect(**connect_kwargs)
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
