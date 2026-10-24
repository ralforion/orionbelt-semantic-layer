"""ob-mysql — OrionBelt Semantic Layer driver for MySQL 8.0+ (PEP 249 DB-API 2.0).

MySQL is read through the ADBC MySQL driver (Arrow-native, from the ADBC
Driver Foundry) when it is installed, and through PyMySQL otherwise. OBML
queries are compiled to MySQL SQL via the OrionBelt REST API running in
single-model mode.

The ADBC driver is not on PyPI; it installs with ``dbc install mysql`` (``pip
install dbc``), and the OrionBelt Docker images carry it. Both clients return
one Arrow schema for a result (see :mod:`ob_mysql.arrow_types`).

PyMySQL is the fallback rather than mysql-connector-python because concurrent
fetches stay concurrent: mysql-connector-python releases the GIL once or more
per row, and threads fetching at once spent most of their time handing it
over (100k rows: 0.08 s alone, 21 s for ten threads).

Usage::

    import ob_mysql

    conn = ob_mysql.connect(database="mydb", user="me", password="secret")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote, urlencode

from ob_mysql.connection import Backend, Connection
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
paramstyle = "format"  # %s placeholders, escaped on the client by either backend

#: The name the ADBC driver manager finds the MySQL driver's manifest under.
ADBC_DRIVER = "mysql"


def _adbc_uri(
    *, host: str, port: int, database: str, user: str | None, password: str | None, charset: str
) -> str:
    """The ADBC driver's ``mysql://`` URI.

    ``tls=preferred`` encrypts whenever the server offers TLS, without
    verifying its certificate, as PyMySQL and the MySQL clients do by default;
    the driver's own default is no TLS at all.
    """
    userinfo = ""
    if user is not None:
        userinfo = quote(user, safe="")
        if password is not None:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    query = urlencode({"tls": "preferred", "charset": charset})
    return f"mysql://{userinfo}{host}:{port}/{quote(database, safe='')}?{query}"


def _connect_adbc(uri: str) -> Any | None:
    """An ADBC connection, or ``None`` when the driver is not installed."""
    try:
        import adbc_driver_manager
        import adbc_driver_manager.dbapi
    except ImportError:
        return None
    try:
        native = adbc_driver_manager.dbapi.connect(driver=ADBC_DRIVER, db_kwargs={"uri": uri})
    except adbc_driver_manager.Error as error:
        # The manager's NOT_FOUND: no manifest or library for the driver.
        if error.status_code == adbc_driver_manager.AdbcStatusCode.NOT_FOUND:
            return None
        raise
    # The driver sets the session's time zone to UTC, which would move
    # ``CURRENT_DATE``, ``NOW()`` and every TIMESTAMP away from the server's
    # own zone, the one PyMySQL's session keeps.
    with native.cursor() as cursor:
        cursor.execute("SET time_zone = @@global.time_zone")
    return native


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
    backend: Literal["auto", "adbc", "pymysql"] = "auto",
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
    backend : str
        ``"auto"`` (default) reads through the ADBC MySQL driver when it is
        installed and through PyMySQL otherwise; ``"adbc"`` or ``"pymysql"``
        insists on one. The ADBC driver takes no certificate files, so
        ``"auto"`` with any ``ssl_*`` path uses PyMySQL.
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    ssl = {k: v for k, v in (("ca", ssl_ca), ("cert", ssl_cert), ("key", ssl_key)) if v}
    if backend == "adbc" and ssl:
        raise NotSupportedError(
            "The ADBC MySQL driver takes no certificate files; use backend='pymysql' "
            "or 'auto' with ssl_ca / ssl_cert / ssl_key."
        )

    def open_pymysql() -> Any:
        import pymysql

        return pymysql.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password or "",
            charset=charset,
            ssl=ssl or None,
        )

    native: Any = None
    if backend != "pymysql" and not ssl:
        uri = _adbc_uri(
            host=host, port=port, database=database, user=user, password=password, charset=charset
        )
        native = _connect_adbc(uri)
        if native is None and backend == "adbc":
            raise InterfaceError(
                "The ADBC MySQL driver is not installed: run `pip install dbc` and "
                "`dbc install mysql`, or connect with backend='pymysql'."
            )
    chosen: Backend = "adbc" if native is not None else "pymysql"
    return Connection(
        native if native is not None else open_pymysql(),
        backend=chosen,
        ob_api_url=ob_api_url,
        ob_timeout=ob_timeout,
        charset=charset,
        fallback=open_pymysql if chosen == "adbc" else None,
    )


__all__ = [
    "ADBC_DRIVER",
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
