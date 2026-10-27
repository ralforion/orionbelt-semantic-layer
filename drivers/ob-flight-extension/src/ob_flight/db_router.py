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
from collections import deque
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
    "DataSourceBusyError",
    "close_all_pools",
    "connect",
    "connection_slot",
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


class DataSourceBusyError(TimeoutError):
    """Every connection a data source allows stayed in use for the whole wait."""

    def __init__(self, source: DataSource, limit: int, timeout: float) -> None:
        setting = f"{source.prefix or 'DB_'}MAX_CONNECTIONS"
        super().__init__(
            f"[DATA_SOURCE_BUSY] Data source '{source.name}' has all {limit} of its "
            f"connections in use, and none was freed within {timeout:g} s. Retry later, "
            f"or raise {setting}."
        )


class ConnectionSlots:
    """At most ``limit`` connections of one data source in use at once.

    A thread that finds every slot taken waits its turn: slots go to waiters
    in order of arrival, so a steady stream of new queries cannot starve one
    that came first. A waiter that is not served within ``timeout`` seconds
    gets :class:`DataSourceBusyError`. ``limit`` 0 means no limit.
    """

    def __init__(self, source: DataSource, limit: int, timeout: float) -> None:
        self._source = source
        self.limit = limit
        self._timeout = timeout
        self._lock = threading.Lock()
        self._waiters: deque[threading.Event] = deque()
        self.in_use = 0

    def acquire(self, *, wait: bool = True) -> None:
        """Take a slot, waiting in line for one if all are taken.

        With ``wait=False`` a caller that finds none free is refused at once,
        for best-effort work that must not hold up its thread.

        Raises:
            DataSourceBusyError: No slot was freed within the timeout.
        """
        with self._lock:
            if self.limit <= 0 or (self.in_use < self.limit and not self._waiters):
                self.in_use += 1
                return
            if not wait:
                raise DataSourceBusyError(self._source, self.limit, 0)
            turn = threading.Event()
            self._waiters.append(turn)
        if turn.wait(self._timeout):
            return
        with self._lock:
            # Handed a slot between the timeout and taking the lock.
            if turn.is_set():
                return
            self._waiters.remove(turn)
        raise DataSourceBusyError(self._source, self.limit, self._timeout)

    def release(self) -> None:
        """Free a slot, handing it to the first waiter if there is one."""
        with self._lock:
            if self._waiters:
                # The slot passes on as it is: ``in_use`` does not change.
                self._waiters.popleft().set()
            else:
                self.in_use -= 1


class ConnectionPool:
    """Thread-safe connection pool for a single data source.

    Idle connections are held in a bounded queue.  ``acquire()`` returns a
    pooled connection when available, otherwise creates a new one.
    ``release()`` returns a connection to the pool (or closes it if full).
    ``slots`` bounds the connections of the source in use at once, pooled
    or not (see :func:`connection_slot`).
    """

    def __init__(
        self,
        source: DataSource | str,
        max_size: int = 5,
        max_connections: int = 0,
        acquire_timeout: float = 30.0,
    ) -> None:
        self._source = _source(source)
        if max_connections > 0:
            max_size = min(max_size, max_connections)
        self._pool: queue.Queue[Any] = queue.Queue(maxsize=max_size)
        self.slots = ConnectionSlots(self._source, max_connections, acquire_timeout)

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
            _pools[key] = ConnectionPool(
                pinned,
                max_size=pinned.pool_size(),
                max_connections=pinned.max_connections(),
                acquire_timeout=pinned.acquire_timeout_seconds(),
            )
        return _pools[key]


@contextmanager
def connection_slot(source: DataSource | str, *, wait: bool = True) -> Generator[None, None, None]:
    """Hold one of a data source's connection slots for the block.

    For a connection opened outside the pool, so that it counts against the
    source's ``MAX_CONNECTIONS`` like a pooled one: open it inside the block
    and close it before the block ends.

    ``wait=False`` refuses at once when no slot is free.

    Raises:
        DataSourceBusyError: No slot was freed within the source's
            ``ACQUIRE_TIMEOUT_SECONDS``.
    """
    slots = _get_pool(_source(source)).slots
    slots.acquire(wait=wait)
    try:
        yield
    finally:
        slots.release()


@contextmanager
def get_connection(
    source: DataSource | str, *, wait: bool = True, **overrides: Any
) -> Generator[Any, None, None]:
    """Context manager that acquires a pooled connection to a data source.

    A dialect name means its implicit source. On clean exit the connection
    is returned to the pool. On exception the connection is discarded (closed).
    The connection holds one of the source's slots while in use;
    ``wait=False`` refuses at once when none is free.

    Raises:
        DataSourceBusyError: Every connection the source allows stayed in use
            for its ``ACQUIRE_TIMEOUT_SECONDS``.
    """
    pool = _get_pool(_source(source))
    pool.slots.acquire(wait=wait)
    try:
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
    finally:
        pool.slots.release()


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
