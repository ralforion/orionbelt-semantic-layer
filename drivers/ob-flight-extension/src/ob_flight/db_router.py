"""Vendor database routing for Flight SQL query execution."""

from __future__ import annotations

import contextlib
import importlib
import os
import queue
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


# Dialect name -> Python module name for the OB driver
VENDOR_MAP: dict[str, str] = {
    "bigquery": "ob_bigquery",
    "duckdb": "ob_duckdb",
    "postgres": "ob_postgres",
    "snowflake": "ob_snowflake",
    "clickhouse": "ob_clickhouse",
    "dremio": "ob_dremio",
    "databricks": "ob_databricks",
    "mysql": "ob_mysql",
}

# Environment variable prefixes for vendor credentials
_CREDENTIAL_KEYS: dict[str, list[str]] = {
    "bigquery": [
        "BIGQUERY_PROJECT",
        "BIGQUERY_LOCATION",
        "BIGQUERY_CREDENTIALS_FILE",
    ],
    "duckdb": ["DUCKDB_DATABASE"],
    "postgres": [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DBNAME",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ],
    "snowflake": [
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
        "SNOWFLAKE_WAREHOUSE",
    ],
    "clickhouse": [
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_USERNAME",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_DATABASE",
    ],
    "dremio": [
        "DREMIO_HOST",
        "DREMIO_PORT",
        "DREMIO_USERNAME",
        "DREMIO_PASSWORD",
    ],
    "databricks": [
        "DATABRICKS_SERVER_HOSTNAME",
        "DATABRICKS_HTTP_PATH",
        "DATABRICKS_ACCESS_TOKEN",
    ],
    "mysql": [
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_DATABASE",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
    ],
}

# Env var name -> connect() kwarg name mapping
_ENV_TO_KWARG: dict[str, str] = {
    "BIGQUERY_PROJECT": "project",
    "BIGQUERY_LOCATION": "location",
    "BIGQUERY_CREDENTIALS_FILE": "credentials_file",
    "DUCKDB_DATABASE": "database",
    "POSTGRES_HOST": "host",
    "POSTGRES_PORT": "port",
    "POSTGRES_DBNAME": "dbname",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "password",
    "SNOWFLAKE_ACCOUNT": "account",
    "SNOWFLAKE_USER": "user",
    "SNOWFLAKE_PASSWORD": "password",
    "SNOWFLAKE_DATABASE": "database",
    "SNOWFLAKE_SCHEMA": "schema",
    "SNOWFLAKE_WAREHOUSE": "warehouse",
    "CLICKHOUSE_HOST": "host",
    "CLICKHOUSE_PORT": "port",
    "CLICKHOUSE_USERNAME": "username",
    "CLICKHOUSE_PASSWORD": "password",
    "CLICKHOUSE_DATABASE": "database",
    "DREMIO_HOST": "host",
    "DREMIO_PORT": "port",
    "DREMIO_USERNAME": "username",
    "DREMIO_PASSWORD": "password",
    "DATABRICKS_SERVER_HOSTNAME": "server_hostname",
    "DATABRICKS_HTTP_PATH": "http_path",
    "DATABRICKS_ACCESS_TOKEN": "access_token",
    "MYSQL_HOST": "host",
    "MYSQL_PORT": "port",
    "MYSQL_DATABASE": "database",
    "MYSQL_USER": "user",
    "MYSQL_PASSWORD": "password",
}


def get_credentials(dialect: str) -> dict[str, Any]:
    """Read vendor credentials from environment variables.

    Returns a dict of kwargs suitable for the vendor's connect() function.
    Only includes env vars that are actually set.
    """
    creds: dict[str, Any] = {}
    keys = _CREDENTIAL_KEYS.get(dialect, [])
    for env_key in keys:
        value = os.getenv(env_key)
        if value is not None:
            kwarg_name = _ENV_TO_KWARG.get(env_key, env_key.lower())
            # Convert port to int
            if env_key.endswith("_PORT"):
                value = int(value)
            creds[kwarg_name] = value
    return creds


def connect(dialect: str, **overrides: Any) -> Any:
    """Connect to a vendor database using the appropriate OB driver.

    Credentials come from environment variables, overridden by **kwargs.
    Returns a PEP 249 Connection object.

    Raises KeyError if dialect is not supported.
    """
    if dialect not in VENDOR_MAP:
        raise KeyError(f"Unsupported dialect: {dialect!r}. Supported: {sorted(VENDOR_MAP)}")
    module = importlib.import_module(VENDOR_MAP[dialect])
    kwargs = get_credentials(dialect)
    kwargs.update(overrides)
    # DuckDB: open read-only to avoid cross-process file lock conflicts
    if dialect == "duckdb" and "read_only" not in kwargs:
        kwargs["read_only"] = True
    return module.connect(**kwargs)


# ---------------------------------------------------------------------------
# Connection pool — reuses connections per dialect to avoid connection storms
# ---------------------------------------------------------------------------


class ConnectionPool:
    """Thread-safe connection pool for a single dialect.

    Idle connections are held in a bounded queue.  ``acquire()`` returns a
    pooled connection when available, otherwise creates a new one.
    ``release()`` returns a connection to the pool (or closes it if full).
    """

    def __init__(self, dialect: str, max_size: int = 5) -> None:
        self._dialect = dialect
        self._pool: queue.Queue[Any] = queue.Queue(maxsize=max_size)

    def acquire(self, **overrides: Any) -> Any:
        """Get a connection from the pool or create a new one."""
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            return connect(self._dialect, **overrides)

    def release(self, conn: Any) -> None:
        """Return a connection to the pool (closes it if pool is full)."""
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            with contextlib.suppress(Exception):
                conn.close()


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _get_pool(dialect: str) -> ConnectionPool:
    """Get or create the pool for a given dialect."""
    with _pools_lock:
        if dialect not in _pools:
            max_size = int(os.getenv("DB_POOL_SIZE", "5"))
            _pools[dialect] = ConnectionPool(dialect, max_size=max_size)
        return _pools[dialect]


@contextmanager
def get_connection(dialect: str, **overrides: Any) -> Generator[Any, None, None]:
    """Context manager that acquires a pooled connection.

    On clean exit the connection is returned to the pool.
    On exception the connection is discarded (closed).
    """
    pool = _get_pool(dialect)
    conn = pool.acquire(**overrides)
    ok = False
    try:
        yield conn
        ok = True
    finally:
        if ok:
            pool.release(conn)
        else:
            with contextlib.suppress(Exception):
                conn.close()


def close_all_pools() -> None:
    """Drain all pools and close every idle connection (for shutdown)."""
    with _pools_lock:
        for pool in _pools.values():
            while True:
                try:
                    conn = pool._pool.get_nowait()
                    with contextlib.suppress(Exception):
                        conn.close()
                except queue.Empty:
                    break
        _pools.clear()
