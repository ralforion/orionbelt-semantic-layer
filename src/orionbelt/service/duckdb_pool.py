"""A DuckDB data source's queries run on Hardcharger workers: the execution pool.

A DuckDB source runs in this process unless its ``DB_<NAME>_DUCKDB_BACKEND``
is ``hardcharger``. Its queries then go to the Hardcharger servers its
``DB_<NAME>_HARDCHARGER_WORKERS`` lists: DuckDB servers that stream Arrow
results over Chargewire, through ``adbc-driver-hardcharger`` (the
``hardcharger`` extra). The SQL stays DuckDB's, so every surface (REST,
pgwire, Flight SQL), ``EXPLAIN``, online validation and value search run it
there alike. Nothing of this module runs for a source in this process: no
driver import, no thread, no connection.

The pool, per source:

- **Picks a worker** for each query, the healthy one with the fewest
  connections in use (the workers in turn on a tie), and holds the connection
  until the query's cursor closes, the whole result read. At most
  ``HARDCHARGER_CONNECTIONS_PER_WORKER`` connections of a worker are in use at
  once: keep it within the worker's statement concurrency plus its queue.
- **Retries what never started.** A worker that cannot be reached, a
  session the worker no longer has (``session_not_found``: it restarted, or
  evicted the idle session), a full statement queue (``rate_limited``) and a
  worker shutting down (``server_draining``) run the query again, on another
  worker where one is free. Anything else is the query's own failure and is
  not run again.
- **Opens each session the same way.** The driver's own reconnect is off: it
  would carry on in a fresh session without ``DUCKDB_CONNECTION_SQL``
  (``SET search_path``), so a lost session is replaced by a new connection
  that runs those statements again.
- **Watches deadlines.** Each statement carries the server-side timeout
  ``HARDCHARGER_QUERY_TIMEOUT_SECONDS``. A worker that dies without closing its
  connections sends nothing more, and the driver has no read timeout, so a
  statement unanswered :data:`WATCHDOG_GRACE_SECONDS` past its deadline is
  ended by closing its cursor, and its worker is taken out.
- **Checks the workers' health** every ``HARDCHARGER_HEALTH_INTERVAL_SECONDS``
  (``GET /chargewire/v1/health``, which needs no sign-in). A worker that does
  not answer ``serving`` takes no queries until it answers again. One that
  answers otherwise keeps its running statements: a worker shutting down
  finishes them. The statements on one that does not answer two checks in a
  row are ended.

A source on the workers reads what the workers' init SQL sets up and its
user's grants allow, so the settings of a source in this process that set
those up here (``DUCKDB_INIT_SQL``, ``DUCKDB_ALLOWED_PATHS``, the memory and
spill settings) are refused for it rather than ignored.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import itertools
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from orionbelt.service.db_executor import ExecutionUnavailableError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

BACKEND_VARIABLE = "DUCKDB_BACKEND"
EMBEDDED = "embedded"
HARDCHARGER = "hardcharger"
DEFAULT_PORT = 31337
DEFAULT_CONNECTIONS_PER_WORKER = 4
DEFAULT_QUERY_TIMEOUT_SECONDS = 120.0
DEFAULT_HEALTH_INTERVAL_SECONDS = 5.0
#: How long opening a connection (discovery, sign-in, session) may take. The
#: driver has no timeout of its own, and a worker that stopped answering with
#: its port still open holds it forever.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
#: How long past its server-side timeout a statement may go unanswered before
#: the pool ends it. The server answers a timed-out statement itself; one that
#: says nothing for this long more is on a worker that stopped answering.
WATCHDOG_GRACE_SECONDS = 10.0
#: How often the pool's thread looks at the deadlines.
WATCH_INTERVAL_SECONDS = 0.5
HEALTH_TIMEOUT_SECONDS = 2.0
#: The pause before running a query refused by a full queue again.
RATE_LIMITED_PAUSE_SECONDS = 0.05

_TIMEOUT_OPTION = "adbc.chargewire.statement.timeout_seconds"
_RECONNECT_OPTION = "adbc.chargewire.session.reconnect"
#: Chargewire codes of a statement that never ran (its session is gone, or
#: the worker's queue was full), so running it again is safe.
_SESSION_GONE = "session_not_found"
_QUEUE_FULL = "rate_limited"
#: A worker shutting down refuses new statements and finishes the running ones.
_DRAINING = "server_draining"
#: Health check answers: ``serving``; answered, but not serving (draining, or
#: still starting); no answer at all.
SERVING, NOT_SERVING, SILENT = "serving", "not_serving", "silent"
#: Health checks in a row with no answer before the statements running on the
#: worker are ended: one missed check is not yet a dead worker.
SILENT_CHECKS_TO_END = 2

#: Settings of a source in this process that do not apply on the workers, and
#: where the workers set the same up instead.
_IN_PROCESS_ONLY: dict[str, str] = {
    "DUCKDB_DATABASE": "the workers' database (their init SQL attaches what they read)",
    "MOTHERDUCK_ACCESS_TOKEN": "the workers' init SQL",
    "DUCKDB_INIT_SQL": "the workers' init SQL",
    "DUCKDB_ALLOWED_PATHS": "the location grants of the source's user in the workers' init SQL",
    "DUCKDB_MEMORY_LIMIT": "the workers' HARDCHARGER_MEMORY_LIMIT",
    "DUCKDB_THREADS": "the workers' CPU limit",
    "DUCKDB_SPILL_DIR": "the workers' own temporary directory",
    "DUCKDB_SPILL_MAX_MB": "the workers' own temporary directory",
}


class PoolSource(Protocol):
    """What the pool reads of a data source (``ob_flight.datasources.DataSource``)."""

    @property
    def name(self) -> str: ...

    @property
    def prefix(self) -> str: ...

    def getenv(self, variable: str) -> str | None: ...


class PoolConfigError(ExecutionUnavailableError):
    """A source on the workers is not set up so that it can run."""


class WorkersUnavailableError(ExecutionUnavailableError):
    """No worker of a source answers, or every connection stayed in use for the wait."""


class WorkerTimeoutError(Exception):
    """A worker did not answer a statement within its deadline and grace."""


class _ConnectTimeoutError(Exception):
    """Opening a connection to a worker did not come back in time."""


@dataclass(frozen=True)
class Worker:
    """One Hardcharger server of a source.

    Attributes:
        uri: The driver's address, as listed: ``hardcharger://host[:port][?tls=false]``.
        name: ``host:port``, for messages.
        health_url: Its ``/chargewire/v1/health``, over the same scheme.
    """

    uri: str
    name: str
    health_url: str


@dataclass(frozen=True)
class PoolSettings:
    """How a source reaches its workers."""

    workers: tuple[Worker, ...]
    user: str
    password: str = field(repr=False)
    connection_sql: str | None = field(default=None, repr=False)
    tls_root_certs: str | None = None
    connections_per_worker: int = DEFAULT_CONNECTIONS_PER_WORKER
    query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    acquire_timeout_seconds: float = 30.0

    def key(self) -> str:
        """What tells one pool from another, as 16 hex characters (secrets included)."""
        payload = json.dumps(dataclasses.astuple(self), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def backend(source: PoolSource | str) -> str:
    """Where a DuckDB source's queries run: :data:`EMBEDDED` or :data:`HARDCHARGER`.

    A source's own ``DB_<NAME>_DUCKDB_BACKEND``, with no fallback to the
    unprefixed variable: which sources leave the process is said per source.
    The unprefixed ``DUCKDB_BACKEND`` is the implicit source's, which
    :func:`pool_settings` refuses. A dialect name means its implicit source.

    Raises:
        PoolConfigError: The setting names neither.
    """
    if isinstance(source, str):
        return EMBEDDED
    environ: Mapping[str, str] = getattr(source, "environ", os.environ)
    raw = environ.get(f"{source.prefix}{BACKEND_VARIABLE}")
    value = (raw or EMBEDDED).strip().lower()
    if value not in (EMBEDDED, HARDCHARGER):
        raise PoolConfigError(
            f"{_variable(source, BACKEND_VARIABLE)} is {raw!r}; it is '{EMBEDDED}' "
            f"(the default: DuckDB in this process) or '{HARDCHARGER}' (the workers)."
        )
    return value


def uses_pool(source: PoolSource | str) -> bool:
    """Whether a DuckDB source's queries run on Hardcharger workers.

    Raises:
        PoolConfigError: The backend setting names neither backend.
    """
    return backend(source) == HARDCHARGER


def pooled[S: PoolSource](source: S | str) -> S | None:
    """The source, when its queries run on Hardcharger workers; ``None`` in process.

    Raises:
        PoolConfigError: The backend setting names neither backend.
    """
    if isinstance(source, str) or not uses_pool(source):
        return None
    return source


def pool_settings(source: PoolSource) -> PoolSettings:
    """How a source on the workers reaches them, from its variables.

    Raises:
        PoolConfigError: A setting is missing or invalid, or the source also
            sets up DuckDB in this process.
    """
    if not source.prefix:
        raise PoolConfigError(
            f"{BACKEND_VARIABLE}={HARDCHARGER} is set for the implicit DuckDB source. Run a "
            "named source on the workers instead: list it in OBSL_DATABASES and set "
            f"DB_<NAME>_{BACKEND_VARIABLE}={HARDCHARGER}."
        )
    environ: Mapping[str, str] = getattr(source, "environ", os.environ)
    for variable, instead in _IN_PROCESS_ONLY.items():
        if environ.get(f"{source.prefix}{variable}") is not None:
            raise PoolConfigError(
                f"{source.prefix}{variable} does not apply to data source '{source.name}', "
                f"whose queries run on Hardcharger workers ({source.prefix}{BACKEND_VARIABLE}="
                f"{HARDCHARGER}). Set it up in {instead} instead."
            )
    listed = [w for w in (source.getenv("HARDCHARGER_WORKERS") or "").split(",") if w.strip()]
    if not listed:
        raise PoolConfigError(
            f"Data source '{source.name}' runs on Hardcharger workers, but "
            f"{_variable(source, 'HARDCHARGER_WORKERS')} lists none: give their addresses, "
            "comma-separated, as hardcharger://host[:port][?tls=false]."
        )
    workers = tuple(
        _worker(raw.strip(), _variable(source, "HARDCHARGER_WORKERS")) for raw in listed
    )
    names = [w.name for w in workers]
    if len(set(names)) != len(names):
        raise PoolConfigError(f"{_variable(source, 'HARDCHARGER_WORKERS')} lists a worker twice.")
    user = source.getenv("HARDCHARGER_USER")
    password = source.getenv("HARDCHARGER_PASSWORD")
    if not user or password is None:
        raise PoolConfigError(
            f"Data source '{source.name}' runs on Hardcharger workers, but "
            f"{_variable(source, 'HARDCHARGER_USER')} and "
            f"{_variable(source, 'HARDCHARGER_PASSWORD')} are not both set: the user the "
            "workers' init SQL creates for this source."
        )
    certs = source.getenv("HARDCHARGER_TLS_ROOT_CERTS") or None
    if certs is not None and not os.path.isfile(certs):
        raise PoolConfigError(
            f"{_variable(source, 'HARDCHARGER_TLS_ROOT_CERTS')} names {certs!r}, "
            "which is not a file."
        )
    acquire = getattr(source, "acquire_timeout_seconds", None)
    return PoolSettings(
        workers=workers,
        user=user,
        password=password,
        connection_sql=(source.getenv("DUCKDB_CONNECTION_SQL") or "").strip() or None,
        tls_root_certs=certs,
        connections_per_worker=int(
            _positive(
                source, "HARDCHARGER_CONNECTIONS_PER_WORKER", DEFAULT_CONNECTIONS_PER_WORKER, int
            )
        ),
        query_timeout_seconds=_positive(
            source, "HARDCHARGER_QUERY_TIMEOUT_SECONDS", DEFAULT_QUERY_TIMEOUT_SECONDS, float
        ),
        health_interval_seconds=_positive(
            source, "HARDCHARGER_HEALTH_INTERVAL_SECONDS", DEFAULT_HEALTH_INTERVAL_SECONDS, float
        ),
        connect_timeout_seconds=_positive(
            source, "HARDCHARGER_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS, float
        ),
        acquire_timeout_seconds=float(acquire()) if callable(acquire) else 30.0,
    )


def check_sources(sources: Iterable[Any]) -> None:
    """Fail now for a DuckDB source on the workers that cannot run, not at its first query.

    For each DuckDB source set to :data:`HARDCHARGER`: its settings, and the
    driver installed. Sources in this process are not looked at further.

    Raises:
        PoolConfigError: The first such source that cannot run, by name.
    """
    for source in sources:
        if getattr(source, "dialect", None) == "duckdb" and uses_pool(source):
            pool_settings(source)
            _driver(source)


def _worker(raw: str, variable: str) -> Worker:
    parts = urllib.parse.urlsplit(raw)
    try:
        port = DEFAULT_PORT if parts.port is None else parts.port
    except ValueError:
        port = -1
    if parts.scheme != "hardcharger" or not parts.hostname or port <= 0:
        raise PoolConfigError(
            f"{variable}: {raw!r} is not a worker address, hardcharger://host[:port][?tls=false]."
        )
    flags = urllib.parse.parse_qs(parts.query)
    tls = flags.get("tls", ["true"])[-1].strip().lower() not in ("false", "0", "no", "off")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    scheme = "https" if tls else "http"
    return Worker(
        uri=raw,
        name=f"{host}:{port}",
        health_url=f"{scheme}://{host}:{port}/chargewire/v1/health",
    )


def _positive(source: PoolSource, variable: str, default: float, kind: type) -> float:
    raw = source.getenv(variable)
    if raw is None or not raw.strip():
        return default
    try:
        value = kind(raw.strip())
    except ValueError:
        value = 0
    if value <= 0:
        raise PoolConfigError(f"{_variable(source, variable)} is {raw!r}; it is a positive number.")
    return float(value)


def _variable(source: PoolSource, variable: str) -> str:
    return f"{source.prefix or 'DB_<NAME>_'}{variable}"


def _driver(source: PoolSource) -> Any:
    """The driver's DB-API module, imported only for a source on the workers."""
    try:
        import adbc_driver_hardcharger.dbapi as dbapi
    except ImportError:
        raise PoolConfigError(
            f"Data source '{source.name}' runs on Hardcharger workers, but their driver is "
            "not installed: install OBSL with its hardcharger extra "
            "(pip install 'orionbelt-semantic-layer[hardcharger]')."
        ) from None
    return dbapi


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def _chargewire_code(exc: BaseException) -> str | None:
    """The Chargewire code a driver error carries (``session_not_found``, ...)."""
    for key, value in getattr(exc, "details", None) or ():
        name = key.decode() if isinstance(key, bytes) else str(key)
        if name == "chargewire.code":
            return value.decode() if isinstance(value, bytes) else str(value)
    return None


def _unreachable(exc: BaseException) -> bool:
    """Whether a driver error means the worker could not be reached at all.

    The request never got to a server, so nothing ran: the driver reports
    the network error of its dial, with no Chargewire code.
    """
    text = str(exc)
    return _chargewire_code(exc) is None and (
        "dial tcp" in text or "connection refused" in text or "no such host" in text
    )


class _WorkerState:
    """A worker as the pool sees it; guarded by the pool's condition."""

    def __init__(self, worker: Worker, index: int) -> None:
        self.worker = worker
        self.index = index
        self.healthy = True
        self.silent_checks = 0
        self.in_use = 0
        self.idle: list[Any] = []


class _Statement:
    """A statement running on a worker, for the watchdog."""

    def __init__(self, state: _WorkerState, cursor: Any, deadline: float) -> None:
        self.state = state
        self.cursor = cursor
        self.deadline = deadline
        self.ended_by_pool: str | None = None


class Pool:
    """The workers of one data source, and the connections open to them."""

    def __init__(self, source_name: str, settings: PoolSettings, driver: Any) -> None:
        self._source_name = source_name
        self._settings = settings
        self._driver = driver
        self._workers = [_WorkerState(w, i) for i, w in enumerate(settings.workers)]
        self._turn = itertools.count()
        self._cond = threading.Condition()
        self._running: set[_Statement] = set()
        self._stop = threading.Event()
        self._ssl = (
            ssl.create_default_context(cafile=settings.tls_root_certs)
            if settings.tls_root_certs
            else None
        )
        self._thread = threading.Thread(
            target=self._watch, name=f"obsl-duckdb-pool-{source_name}", daemon=True
        )
        self._thread.start()

    # -- queries ----------------------------------------------------------

    def cursor(self) -> PoolCursor:
        """A cursor whose ``execute`` runs on a worker of this pool."""
        return PoolCursor(self)

    def execute(self, sql: str) -> tuple[_Lease, Any]:
        """Run ``sql`` on a worker: the lease that holds it, and the driver's cursor.

        The caller reads the result from the cursor and then closes the lease,
        which closes the cursor and gives the connection back.

        Raises:
            WorkersUnavailableError: No worker answers, or every connection
                stayed in use for the whole wait.
            WorkerTimeoutError: The worker stopped answering the statement.
            Exception: The driver's error for the query's own failure.
        """
        deadline = time.monotonic() + self._settings.acquire_timeout_seconds
        avoid: _WorkerState | None = None
        while True:
            state, conn = self._acquire(deadline, avoid)
            try:
                cursor = conn.cursor()
            except Exception:
                self._release(state, conn, keep=False)
                raise
            statement = self._track(state, cursor)
            try:
                self._run(cursor, sql)
            except Exception as exc:
                self._forget(statement)
                with contextlib.suppress(Exception):
                    cursor.close()
                retry = self._after_failure(state, conn, statement, exc)
                if retry is None:
                    raise
                avoid = state if retry == _QUEUE_FULL else None
                if time.monotonic() >= deadline:
                    raise WorkersUnavailableError(self._busy_message()) from None
                if retry == _QUEUE_FULL:
                    time.sleep(RATE_LIMITED_PAUSE_SECONDS)
                continue
            return _Lease(self, state, conn, statement), cursor

    def _track(self, state: _WorkerState, cursor: Any) -> _Statement:
        """Put a statement about to run under the watchdog."""
        statement = _Statement(
            state,
            cursor,
            time.monotonic() + self._settings.query_timeout_seconds + WATCHDOG_GRACE_SECONDS,
        )
        with self._cond:
            self._running.add(statement)
        return statement

    def _run(self, cursor: Any, sql: str) -> None:
        """Run a statement with its timeout set on the worker."""
        cursor.adbc_statement.set_options(
            **{_TIMEOUT_OPTION: str(max(1, round(self._settings.query_timeout_seconds)))}
        )
        cursor.execute(sql)

    def _after_failure(
        self, state: _WorkerState, conn: Any, statement: _Statement, exc: BaseException
    ) -> str | None:
        """Settle a failed statement's connection; what to retry, or ``None`` to raise."""
        if statement.ended_by_pool is not None:
            self._release(state, conn, keep=False)
            raise WorkerTimeoutError(statement.ended_by_pool) from exc
        code = _chargewire_code(exc)
        if code == _SESSION_GONE:
            # A restart ends every session of the worker, and the idle ones
            # waited longest: open new ones rather than try each in turn.
            self._release(state, conn, keep=False)
            self._drop_idle(state)
            return _SESSION_GONE
        if code == _QUEUE_FULL:
            self._release(state, conn, keep=True)
            return _QUEUE_FULL
        if code == _DRAINING:
            self._release(state, conn, keep=False)
            self._take_out(state, "it is shutting down")
            return _DRAINING
        if _unreachable(exc):
            self._release(state, conn, keep=False)
            self._take_out(state, f"unreachable: {exc}")
            return "unreachable"
        # The query's own failure; the session is still good.
        self._release(state, conn, keep=True)
        return None

    # -- connections ------------------------------------------------------

    def _acquire(self, deadline: float, avoid: _WorkerState | None) -> tuple[_WorkerState, Any]:
        """A worker with a connection free, and the connection (opened if none is idle).

        Opening one fails like a statement does. A full queue (the connection
        statements are queued too) tries another worker, a lost session opens
        another, and a worker that cannot be reached, is shutting down, or did
        not answer in time is taken out. None of it ran the query.
        """
        while True:
            state, conn = self._reserve(deadline, avoid)
            if conn is not None:
                return state, conn
            try:
                return state, self._open(state)
            except PoolConfigError:
                self._release(state, None, keep=False)
                raise
            except Exception as exc:
                self._release(state, None, keep=False)
                code = _chargewire_code(exc)
                if code == _QUEUE_FULL:
                    avoid = state
                    time.sleep(RATE_LIMITED_PAUSE_SECONDS)
                elif code != _SESSION_GONE and not isinstance(exc, WorkerTimeoutError):
                    # (The watchdog took out a worker whose setup it ended.)
                    self._take_out(state, f"could not connect: {exc}")
                if time.monotonic() >= deadline:
                    message = self._busy_message() if code == _QUEUE_FULL else None
                    raise WorkersUnavailableError(message or self._unavailable_message()) from None

    def _reserve(
        self, deadline: float, avoid: _WorkerState | None
    ) -> tuple[_WorkerState, Any | None]:
        """Count a connection in use on a worker; an idle one of it, if any."""
        limit = self._settings.connections_per_worker
        with self._cond:
            while True:
                if self._stop.is_set():
                    raise WorkersUnavailableError(
                        f"Data source '{self._source_name}': the pool is shut down."
                    )
                healthy = [s for s in self._workers if s.healthy]
                if not healthy:
                    raise WorkersUnavailableError(self._unavailable_message())
                free = [s for s in healthy if s.in_use < limit]
                if free:
                    choices = [s for s in free if s is not avoid] or free
                    start = next(self._turn) % len(self._workers)
                    state = min(
                        choices,
                        key=lambda s: (s.in_use, (s.index - start) % len(self._workers)),
                    )
                    state.in_use += 1
                    return state, (state.idle.pop() if state.idle else None)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkersUnavailableError(self._busy_message())
                self._cond.wait(remaining)

    def _open(self, state: _WorkerState) -> Any:
        """A new connection to a worker, its session set up by ``DUCKDB_CONNECTION_SQL``.

        The statements run like a query: with the statement timeout, under the
        watchdog, and ended with the worker's other statements when it stops
        answering.

        Raises:
            PoolConfigError: The connection statements failed on their own.
            WorkerTimeoutError: The pool ended them: the worker stopped answering.
            Exception: The driver's error for a worker that cannot be reached,
                a full queue, a session gone, or a worker shutting down.
        """
        conn = self._connect(state.worker)
        sql = self._settings.connection_sql
        if not sql:
            return conn
        try:
            cursor = conn.cursor()
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            raise
        statement = self._track(state, cursor)
        try:
            self._run(cursor, sql)
        except Exception as exc:
            # The cursor first: the driver refuses to close a connection with one open.
            with contextlib.suppress(Exception):
                cursor.close()
            with contextlib.suppress(Exception):
                conn.close()
            if statement.ended_by_pool is not None:
                raise WorkerTimeoutError(statement.ended_by_pool) from None
            if _chargewire_code(exc) in (_QUEUE_FULL, _SESSION_GONE, _DRAINING) or _unreachable(
                exc
            ):
                raise
            # Without the chained error: it would carry the statements.
            raise PoolConfigError(
                f"Data source '{self._source_name}': DUCKDB_CONNECTION_SQL failed on "
                f"worker {state.worker.name}: {type(exc).__name__}."
            ) from None
        finally:
            self._forget(statement)
            with contextlib.suppress(Exception):
                cursor.close()
        return conn

    def _connect(self, worker: Worker) -> Any:
        """Open a connection, giving up after ``HARDCHARGER_CONNECT_TIMEOUT_SECONDS``.

        The driver's connect cannot be interrupted, so it runs in a thread of
        its own. One that comes back after the pool gave up closes what it
        opened.

        Raises:
            _ConnectTimeoutError: It did not come back in time.
            Exception: The driver's error.
        """
        done = threading.Event()
        lock = threading.Lock()
        outcome: dict[str, Any] = {}

        def connect() -> None:
            try:
                conn = self._driver.connect(
                    uri=worker.uri,
                    username=self._settings.user,
                    password=self._settings.password,
                    tls_root_certs=self._settings.tls_root_certs,
                    db_kwargs={_RECONNECT_OPTION: "false"},
                )
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread
                with lock:
                    outcome["error"] = exc
            else:
                with lock:
                    late = outcome.get("abandoned", False)
                    if not late:
                        outcome["conn"] = conn
                if late:
                    with contextlib.suppress(Exception):
                        conn.close()
            finally:
                done.set()

        threading.Thread(
            target=connect, name=f"obsl-duckdb-connect-{worker.name}", daemon=True
        ).start()
        if not done.wait(self._settings.connect_timeout_seconds):
            with lock:
                outcome["abandoned"] = True
                conn = outcome.pop("conn", None)
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            raise _ConnectTimeoutError(
                f"opening a connection did not come back within "
                f"{self._settings.connect_timeout_seconds:g} s"
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["conn"]

    def _release(self, state: _WorkerState, conn: Any | None, *, keep: bool) -> None:
        """Give a connection back: idle for the next query, or closed."""
        stale = None
        with self._cond:
            state.in_use -= 1
            if conn is not None:
                if keep and state.healthy and not self._stop.is_set():
                    state.idle.append(conn)
                else:
                    stale = conn
            self._cond.notify()
        if stale is not None:
            with contextlib.suppress(Exception):
                stale.close()

    def _drop_idle(self, state: _WorkerState) -> None:
        with self._cond:
            idle, state.idle = state.idle, []
        for conn in idle:
            with contextlib.suppress(Exception):
                conn.close()

    def _forget(self, statement: _Statement) -> None:
        with self._cond:
            self._running.discard(statement)

    # -- health -----------------------------------------------------------

    def _take_out(self, state: _WorkerState, reason: str, *, end_running: bool = False) -> None:
        """Send no more queries to a worker until its health check passes.

        Its idle connections close. ``end_running`` ends the statements running
        on it too: for a worker that has stopped answering, whose reads would
        otherwise wait for their deadlines. A worker that still answers (a
        draining one finishes its running statements) keeps them.
        """
        with self._cond:
            was = state.healthy
            state.healthy = False
            idle, state.idle = state.idle, []
            running = [s for s in self._running if s.state is state] if end_running else []
        if was:
            logger.warning(
                "DuckDB pool of '%s': worker %s taken out (%s)",
                self._source_name,
                state.worker.name,
                reason,
            )
        for conn in idle:
            with contextlib.suppress(Exception):
                conn.close()
        for statement in running:
            self._end(statement, f"worker {state.worker.name} stopped answering its health checks")

    def _end(self, statement: _Statement, reason: str) -> None:
        """End a statement from this thread by closing its cursor.

        A cancel would be sent to the same unresponsive worker and wait with
        it; closing the cursor ends a blocked read or ``execute`` at once.
        """
        if statement.ended_by_pool is not None:
            return
        statement.ended_by_pool = (
            f"[WORKER_UNRESPONSIVE] Data source '{self._source_name}': {reason}; the query "
            "was ended, and the worker takes no queries until it answers again."
        )
        with contextlib.suppress(Exception):
            statement.cursor.close()

    def _health(self, worker: Worker) -> str:
        """:data:`SERVING`, :data:`NOT_SERVING` (it answered otherwise), or :data:`SILENT`."""
        try:
            with urllib.request.urlopen(  # noqa: S310 - the scheme is http or https, from _worker
                worker.health_url, timeout=HEALTH_TIMEOUT_SECONDS, context=self._ssl
            ) as response:
                status = json.loads(response.read()).get("status")
        except urllib.error.HTTPError:
            return NOT_SERVING  # it answered, with an error status
        except (OSError, ValueError):
            return SILENT
        return SERVING if status == "serving" else NOT_SERVING

    def _check_health(self) -> None:
        for state in self._workers:
            if self._stop.is_set():
                return
            health = self._health(state.worker)
            if health == SERVING:
                if not state.healthy:
                    logger.info(
                        "DuckDB pool of '%s': worker %s is back",
                        self._source_name,
                        state.worker.name,
                    )
                with self._cond:
                    state.healthy = True
                    state.silent_checks = 0
                    self._cond.notify_all()
            elif health == NOT_SERVING:
                state.silent_checks = 0
                self._take_out(state, "its health check answers 'not_serving'")
            else:
                state.silent_checks += 1
                self._take_out(
                    state,
                    "its health check does not answer",
                    end_running=state.silent_checks >= SILENT_CHECKS_TO_END,
                )

    def _watch(self) -> None:
        """The pool's thread: deadlines every half second, health every interval."""
        next_health = 0.0
        while not self._stop.wait(WATCH_INTERVAL_SECONDS):
            now = time.monotonic()
            with self._cond:
                late = [s for s in self._running if now > s.deadline and s.ended_by_pool is None]
            for statement in late:
                self._end(
                    statement,
                    f"worker {statement.state.worker.name} did not answer within "
                    f"{self._settings.query_timeout_seconds:g} s and "
                    f"{WATCHDOG_GRACE_SECONDS:g} s more",
                )
                self._take_out(statement.state, "a statement went unanswered past its deadline")
            if now >= next_health:
                self._check_health()
                next_health = time.monotonic() + self._settings.health_interval_seconds

    # -- messages ---------------------------------------------------------

    def _unavailable_message(self) -> str:
        names = ", ".join(s.worker.name for s in self._workers)
        return (
            f"[WORKERS_UNAVAILABLE] Data source '{self._source_name}': none of its "
            f"Hardcharger workers ({names}) answers. Queries resume when one passes its "
            "health check."
        )

    def _busy_message(self) -> str:
        return (
            f"[DATA_SOURCE_BUSY] Data source '{self._source_name}' has all "
            f"{self._settings.connections_per_worker} connections of each healthy worker in "
            f"use, and none was freed within {self._settings.acquire_timeout_seconds:g} s. "
            "Retry later, or add a worker."
        )

    def close(self) -> None:
        """Stop the thread and close the idle connections; leases still out close on release."""
        self._stop.set()
        with self._cond:
            idle = [conn for s in self._workers for conn in s.idle]
            for s in self._workers:
                s.idle = []
            self._cond.notify_all()
        for conn in idle:
            with contextlib.suppress(Exception):
                conn.close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=HEALTH_TIMEOUT_SECONDS * (len(self._workers) + 1))


class _Lease:
    """A connection of a worker held by one statement, until :meth:`close`."""

    def __init__(self, pool: Pool, state: _WorkerState, conn: Any, statement: _Statement) -> None:
        self._pool = pool
        self._state = state
        self._conn = conn
        self._statement = statement
        self._closed = False

    @property
    def worker(self) -> Worker:
        return self._state.worker

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool._forget(self._statement)
        with contextlib.suppress(Exception):
            self._statement.cursor.close()
        ended = self._statement.ended_by_pool is not None
        self._pool._release(self._state, self._conn, keep=not ended)


class PoolCursor:
    """A DB-API cursor whose ``execute`` runs on a worker of a pool.

    It holds its worker's connection from ``execute`` until ``close``, so a
    result is read whole on the connection that produced it, and reading it
    after the watchdog ended it raises :class:`WorkerTimeoutError`.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._lease: _Lease | None = None
        self._cursor: Any = None

    def execute(self, operation: str, parameters: Any = None) -> None:
        if parameters is not None:
            raise NotImplementedError("a pooled DuckDB cursor runs SQL without parameters")
        self.close()
        self._lease, self._cursor = self._pool.execute(operation)

    @property
    def description(self) -> Any:
        return self._cursor.description if self._cursor is not None else None

    def fetchone(self) -> Any:
        return self._read("fetchone")

    def fetchmany(self, size: int | None = None) -> Any:
        return self._read("fetchmany") if size is None else self._read("fetchmany", size)

    def fetchall(self) -> Any:
        return self._read("fetchall")

    def fetch_arrow_table(self) -> Any:
        return self._read("fetch_arrow_table")

    def fetch_record_batch(self) -> Any:
        return self._read("fetch_record_batch")

    def _read(self, method: str, *args: Any) -> Any:
        if self._cursor is None or self._lease is None:
            raise RuntimeError("execute() a statement first")
        statement = self._lease._statement
        try:
            return getattr(self._cursor, method)(*args)
        except Exception as exc:
            if statement.ended_by_pool is not None:
                raise WorkerTimeoutError(statement.ended_by_pool) from exc
            raise

    def close(self) -> None:
        if self._lease is not None:
            self._lease.close()
        self._lease = None
        self._cursor = None

    def __enter__(self) -> PoolCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PoolConnection:
    """A DB-API connection whose cursors run on a pool (Flight SQL's path).

    Closing it closes the cursors it handed out, giving their worker
    connections back.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._cursors: list[PoolCursor] = []

    def cursor(self) -> PoolCursor:
        cursor = self._pool.cursor()
        self._cursors.append(cursor)
        return cursor

    def close(self) -> None:
        for cursor in self._cursors:
            cursor.close()
        self._cursors.clear()

    def __enter__(self) -> PoolConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# One pool per source and settings
# ---------------------------------------------------------------------------

#: Each source's pool, by name, with the key of the settings it was made with.
_POOLS: dict[str, tuple[str, Pool]] = {}
_POOLS_LOCK = threading.Lock()


def pool(source: PoolSource) -> Pool:
    """The pool of a source on the workers, created at its first query.

    A source whose settings changed gets a new pool, and the old one closes:
    its idle connections and its thread now, its connections in use as their
    queries end.

    Raises:
        PoolConfigError: The source cannot run on the workers.
    """
    settings = pool_settings(source)
    key = settings.key()
    replaced = None
    with _POOLS_LOCK:
        entry = _POOLS.get(source.name)
        if entry is not None and entry[0] == key:
            return entry[1]
        found = Pool(source.name, settings, _driver(source))
        _POOLS[source.name] = (key, found)
        if entry is not None:
            replaced = entry[1]
    if replaced is not None:
        replaced.close()
    return found


def cursor(source: PoolSource) -> PoolCursor:
    """A cursor on a source's workers; close it to give its connection back."""
    return pool(source).cursor()


def connection(source: PoolSource) -> PoolConnection:
    """A DB-API connection on a source's workers, for code that wants one."""
    return PoolConnection(pool(source))


def close_pools() -> None:
    """Close every pool, at shutdown."""
    with _POOLS_LOCK:
        pools = [found for _, found in _POOLS.values()]
        _POOLS.clear()
    for found in pools:
        found.close()
