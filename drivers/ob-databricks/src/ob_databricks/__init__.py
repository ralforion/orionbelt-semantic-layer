"""ob-databricks — OrionBelt Semantic Layer driver for Databricks (PEP 249 DB-API 2.0).

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Usage::

    import ob_databricks

    conn = ob_databricks.connect(
        server_hostname="adb-xxx.azuredatabricks.net",
        http_path="/sql/1.0/warehouses/abc123",
        access_token="dapi...",
    )
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any

from databricks import sql

from ob_databricks.connection import Connection
from ob_databricks.exceptions import (
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
paramstyle = "pyformat"  # databricks-sql-connector uses %(name)s


def connect(
    server_hostname: str,
    http_path: str,
    *,
    access_token: str | None = None,
    catalog: str = "hive_metastore",
    schema: str = "default",
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
    **kwargs: Any,  # noqa: ANN401
) -> Connection:
    """Open a Databricks connection with OBML support.

    Parameters
    ----------
    server_hostname : str
        Databricks workspace hostname (e.g. ``adb-xxx.azuredatabricks.net``).
    http_path : str
        SQL warehouse HTTP path (e.g. ``/sql/1.0/warehouses/abc123``).
    access_token : str, optional
        Personal access token or OAuth token.
    catalog : str
        Unity Catalog name (default: ``hive_metastore``).
    schema : str
        Schema name (default: ``default``).
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    **kwargs
        Extra keyword arguments forwarded to ``databricks.sql.connect()``.
    """
    connect_kwargs: dict[str, Any] = {
        "server_hostname": server_hostname,
        "http_path": http_path,
        "catalog": catalog,
        "schema": schema,
    }
    if access_token is not None:
        connect_kwargs["access_token"] = access_token
    connect_kwargs.update(kwargs)

    native = sql.connect(**connect_kwargs)
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
