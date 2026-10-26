"""Vendor database routing for Flight SQL query execution.

Which database a connection goes to is a :class:`~ob_flight.datasources.DataSource`.
A dialect name still works wherever one is taken: it means the dialect's
implicit source, its unprefixed variables, which is how every connection
worked before named sources.
"""

from __future__ import annotations

import contextlib
import importlib
import queue
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from ob_flight.datasources import (
    _CREDENTIAL_KEYS,
    _ENV_ALIASES,
    _ENV_TO_KWARG,
    _MOTHERDUCK_PREFIX,
    _MOTHERDUCK_URI_TOKEN_PARAMS,
    DataSource,
    MotherDuckTokenMissingError,
    _apply_motherduck_token,
    implicit_datasource,
)

# The names this module exports, the moved credential tables included: callers
# and tests import them from here, and mypy needs them listed to see them.
__all__ = [
    "DataSource",
    "MotherDuckTokenMissingError",
    "VENDOR_MAP",
    "ConnectionPool",
    "close_all_pools",
    "connect",
    "get_connection",
    "get_credentials",
    "implicit_datasource",
    "_CREDENTIAL_KEYS",
    "_ENV_ALIASES",
    "_ENV_TO_KWARG",
    "_MOTHERDUCK_PREFIX",
    "_MOTHERDUCK_URI_TOKEN_PARAMS",
    "_apply_motherduck_token",
]

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


def _source(source: DataSource | str) -> DataSource:
    """A data source, or the implicit source of a dialect name."""
    return implicit_datasource(source) if isinstance(source, str) else source


def get_credentials(dialect: str, **overrides: Any) -> dict[str, Any]:
    """Credentials of a dialect's implicit source, as ``connect()`` kwargs.

    See :meth:`~ob_flight.datasources.DataSource.credentials`.
    """
    return implicit_datasource(dialect).credentials(**overrides)


def connect(source: DataSource | str, **overrides: Any) -> Any:
    """Connect to a data source using its dialect's OB driver.

    Credentials come from the source's environment variables, overridden by
    **kwargs. Returns a PEP 249 Connection object.

    Raises KeyError if the dialect is not supported.
    """
    datasource = _source(source)
    dialect = datasource.dialect
    if dialect not in VENDOR_MAP:
        raise KeyError(f"Unsupported dialect: {dialect!r}. Supported: {sorted(VENDOR_MAP)}")
    module = importlib.import_module(VENDOR_MAP[dialect])
    kwargs = datasource.credentials(**overrides)
    # DuckDB: open read-only to avoid cross-process file lock conflicts.
    # MotherDuck is remote, so no local file lock exists to conflict over --
    # but read-only is still correct for a semantic layer that only reads,
    # and a `md:` connection accepts it, so the default is left in place.
    if dialect == "duckdb" and "read_only" not in kwargs:
        kwargs["read_only"] = True
    return module.connect(**kwargs)


# ---------------------------------------------------------------------------
# Connection pool — reuses connections per data source to avoid connection storms
# ---------------------------------------------------------------------------


class ConnectionPool:
    """Thread-safe connection pool for a single data source.

    Idle connections are held in a bounded queue.  ``acquire()`` returns a
    pooled connection when available, otherwise creates a new one.
    ``release()`` returns a connection to the pool (or closes it if full).
    """

    def __init__(self, source: DataSource | str, max_size: int = 5) -> None:
        self._source = _source(source)
        self._pool: queue.Queue[Any] = queue.Queue(maxsize=max_size)

    def acquire(self, **overrides: Any) -> Any:
        """Get a connection from the pool or create a new one."""
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            return connect(self._source, **overrides)

    def release(self, conn: Any) -> None:
        """Return a connection to the pool (closes it if pool is full)."""
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            with contextlib.suppress(Exception):
                conn.close()


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _get_pool(source: DataSource) -> ConnectionPool:
    """Get or create the pool for a data source.

    Keyed by the source's fingerprint, not its name: two names for the same
    target with the same credentials share a pool, and a changed credential
    gets a new one rather than handing out connections signed in as before.

    The pool connects with a snapshot of the settings its key was computed
    from. Bound to the live environment instead, a pool two sources share
    would open its next connection with the first source's settings after
    those changed, and serve the other source another database's rows.
    """
    pinned = source.snapshot()
    key = pinned.fingerprint()
    with _pools_lock:
        if key not in _pools:
            _pools[key] = ConnectionPool(pinned, max_size=pinned.pool_size())
        return _pools[key]


@contextmanager
def get_connection(source: DataSource | str, **overrides: Any) -> Generator[Any, None, None]:
    """Context manager that acquires a pooled connection to a data source.

    A dialect name means its implicit source. On clean exit the connection
    is returned to the pool. On exception the connection is discarded (closed).
    """
    pool = _get_pool(_source(source))
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
