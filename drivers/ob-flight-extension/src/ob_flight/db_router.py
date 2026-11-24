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
import time
from collections import OrderedDict, deque
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from orionbelt import __version__ as _obsl_version

from ob_flight.datasources import (
    _CREDENTIAL_KEYS,
    _ENV_ALIASES,
    _ENV_TO_KWARG,
    _MOTHERDUCK_PREFIX,
    _MOTHERDUCK_URI_TOKEN_PARAMS,
    USER_LOGIN,
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

# The drivers that name OBSL to their database, by ``client_name``. Snowflake
# and Dremio are left out: see ``orionbelt.service.client_name``.
_CLIENT_NAMED = frozenset({"bigquery", "clickhouse", "databricks", "duckdb", "mysql", "postgres"})

# The name ``orionbelt.service.client_name`` gives OBSL, built here from the
# version rather than imported: this package does not require an OBSL release
# recent enough to have that module.
_CLIENT_NAME = f"orionbelt-semantic-layer/{_obsl_version}"


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
    if dialect == "duckdb" and not overrides:
        # OBSL opens a DuckDB source alike on every surface: its setup
        # statements, memory and spill settings, and the instance a source
        # without a database file keeps; or, for a source on Hardcharger
        # workers, a connection whose cursors run there.
        from orionbelt.service import duckdb_pool, duckdb_source

        if duckdb_pool.uses_pool(datasource):
            return duckdb_pool.connection(datasource)

        native = duckdb_source.connect(datasource)
        config = duckdb_source.settings(datasource)

        def prepare(cursor: Any) -> None:
            duckdb_source.prepare(cursor, datasource, config)

        return module.Connection(native, init=prepare)
    if datasource.login == USER_LOGIN:
        # Logged in as the caller: their token for the warehouse, issued by an
        # exchange of the token they called with. Raises when there is no
        # signed-in caller or the warehouse refuses them.
        from orionbelt.service.user_login import connect_overrides

        overrides = {**connect_overrides(datasource), **overrides}
    return _open(datasource, module, overrides)


def _open(datasource: DataSource, module: Any, overrides: dict[str, Any]) -> Any:
    """Open a connection with the source's credentials, ``overrides`` applied."""
    dialect = datasource.dialect
    kwargs = datasource.credentials(**overrides)
    if dialect in _CLIENT_NAMED:
        kwargs.setdefault("client_name", _CLIENT_NAME)
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


class IdleUserConnections:
    """Idle connections of a source that logs in as the user, for each user's next query.

    A connection serves only the user it was opened for, never past
    ``usable_until`` (a minute before its warehouse token expires). Its idle
    limit is the source's own (``USER_CONNECTION_IDLE_SECONDS``), applied as
    it is kept and as it is taken: two names for one warehouse share a pool,
    and each keeps its own limit. One is kept per user, at most ``max_size``
    across users; when full, the least recently used goes. Users are known by
    a hash (``caller_scope``), never by their ``sub``. Expired ones are closed
    by the next take or put, and by the sweep (``sweep_idle_user_connections``).
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        # owner -> (connection, usable_until, deadline, kept at)
        self._idle: OrderedDict[str, tuple[Any, float, float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, owner: str, idle_seconds: float) -> tuple[Any, float] | None:
        """The user's idle connection and its ``usable_until``, if still usable.

        ``idle_seconds`` is the taking source's limit: a connection idle
        longer than it is closed, and with 0 none is taken.
        """
        if idle_seconds <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._idle.pop(owner, None)
            expired = self._pop_expired(now)
        if entry is not None and (entry[2] <= now or now - entry[3] >= idle_seconds):
            expired.append(entry[0])
            entry = None
        _close_all(expired)
        return (entry[0], entry[1]) if entry is not None else None

    def put(self, owner: str, conn: Any, usable_until: float, idle_seconds: float) -> None:
        """Keep ``conn`` for the user's next query for at most ``idle_seconds``, or close it."""
        now = time.monotonic()
        deadline = min(usable_until, now + idle_seconds)
        closing: list[Any] = []
        with self._lock:
            if self._max_size <= 0 or deadline <= now:
                closing.append(conn)
            else:
                previous = self._idle.pop(owner, None)
                if previous is not None:
                    closing.append(previous[0])
                self._idle[owner] = (conn, usable_until, deadline, now)
                while len(self._idle) > self._max_size:
                    closing.append(self._idle.popitem(last=False)[1][0])
            kept = owner in self._idle
            closing += self._pop_expired(now)
        _close_all(closing)
        if kept:
            _start_sweeper()

    def sweep(self) -> None:
        """Close every idle connection past its deadline."""
        with self._lock:
            expired = self._pop_expired(time.monotonic())
        _close_all(expired)

    def close_all(self) -> None:
        """Close every idle connection (shutdown)."""
        with self._lock:
            idle = [entry[0] for entry in self._idle.values()]
            self._idle.clear()
        _close_all(idle)

    def _pop_expired(self, now: float) -> list[Any]:
        expired = [owner for owner, entry in self._idle.items() if entry[2] <= now]
        return [self._idle.pop(owner)[0] for owner in expired]


def _close_all(connections: list[Any]) -> None:
    for conn in connections:
        with contextlib.suppress(Exception):
            conn.close()


#: How often the sweep closes expired idle user connections, in seconds.
_SWEEP_INTERVAL_SECONDS = 60.0
_sweeper_stop: threading.Event | None = None
_sweeper_lock = threading.Lock()


def sweep_idle_user_connections() -> None:
    """Close the idle user connections past their deadline, in every pool.

    Without it, an expired one stayed open on the warehouse until the next
    query on its source, or shutdown: it was never used again, but held a
    warehouse session meanwhile.
    """
    with _pools_lock:
        pools = list(_pools.values())
    for pool in pools:
        pool.users.sweep()


def _start_sweeper() -> None:
    """Run the sweep about every ``_SWEEP_INTERVAL_SECONDS``, once a user connection is kept."""
    global _sweeper_stop  # noqa: PLW0603
    with _sweeper_lock:
        if _sweeper_stop is not None:
            return
        stop = threading.Event()
        _sweeper_stop = stop
    interval = _SWEEP_INTERVAL_SECONDS

    def run() -> None:
        while not stop.wait(interval):
            sweep_idle_user_connections()

    threading.Thread(target=run, name="obsl-user-connection-sweep", daemon=True).start()


def _stop_sweeper() -> None:
    global _sweeper_stop  # noqa: PLW0603
    with _sweeper_lock:
        stop, _sweeper_stop = _sweeper_stop, None
    if stop is not None:
        stop.set()


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
        # A source that logs in as the user keeps its idle connections per user.
        self.users = IdleUserConnections(max_size)

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
    datasource = _source(source)
    pool = _get_pool(datasource)
    # A source that logs in as the caller never shares a connection with
    # another user: its idle connections are kept per user (see
    # ``IdleUserConnections``), and only for calls without overrides.
    per_user = datasource.login == USER_LOGIN
    pool.slots.acquire(wait=wait)
    try:
        owner: str | None = None
        usable_until = 0.0
        # The calling source's own limit, also when it shares the pool.
        idle_seconds = datasource.user_connection_idle_seconds() if per_user else 0.0
        if per_user:
            conn, owner, usable_until = _user_connection(datasource, pool, overrides, idle_seconds)
        else:
            conn = pool.acquire(**overrides)
        ok = False
        try:
            yield conn
            ok = True
        finally:
            if ok and owner is not None:
                pool.users.put(owner, conn, usable_until, idle_seconds)
            elif ok and not per_user:
                pool.release(conn)
            else:
                _close_all([conn])
    finally:
        pool.slots.release()


def _user_connection(
    datasource: DataSource, pool: ConnectionPool, overrides: dict[str, Any], idle_seconds: float
) -> tuple[Any, str | None, float]:
    """A connection logged in as the caller: their idle one, else a new one.

    Returns the connection, the owner to keep it for (None: close it after,
    since it was opened with overrides), and until when it may serve.

    Raises:
        DataSourceSignInError: No signed-in caller, or the warehouse refused them.
    """
    from orionbelt.service.user_login import caller_scope, login_as_caller

    if overrides:
        return connect(datasource, **overrides), None, 0.0
    owner = caller_scope()
    reused = pool.users.take(owner, idle_seconds)
    if reused is not None:
        conn, usable_until = reused
        return conn, owner, usable_until
    login = login_as_caller(datasource)
    module = importlib.import_module(VENDOR_MAP[datasource.dialect])
    return _open(datasource, module, dict(login.overrides)), login.owner, login.usable_until


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
            pool.users.close_all()
        _pools.clear()
    _stop_sweeper()
