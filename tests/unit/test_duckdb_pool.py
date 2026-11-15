"""A DuckDB source's queries run on Hardcharger workers (``duckdb_pool``).

The pool is tested against a stand-in for ``adbc-driver-hardcharger`` that
plays the workers: one that cannot be reached, a session the worker no longer
has, a full statement queue, a statement that hangs. The real driver against
real workers is ``tests/integration/test_duckdb_pool_hardcharger.py``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("ob_flight")
pa = pytest.importorskip("pyarrow")

from ob_flight import db_router  # noqa: E402
from ob_flight.datasources import DataSource  # noqa: E402

from orionbelt.api import app as api_app  # noqa: E402
from orionbelt.service import db_executor, duckdb_pool  # noqa: E402
from orionbelt.service.db_executor import (  # noqa: E402
    ExecutionError,
    ExecutionUnavailableError,
    execute_sql,
    explain_sql,
)
from orionbelt.service.duckdb_pool import (  # noqa: E402
    PoolConfigError,
    WorkersUnavailableError,
    WorkerTimeoutError,
)

W1 = "hardcharger://w1?tls=false"
W2 = "hardcharger://w2?tls=false"


def lake(**variables: str) -> DataSource:
    """A named DuckDB source ``lake`` on the workers, with these ``DB_LAKE_`` variables."""
    settings = {
        "DUCKDB_BACKEND": "hardcharger",
        "HARDCHARGER_WORKERS": f"{W1},{W2}",
        "HARDCHARGER_USER": "obsl_lake",
        "HARDCHARGER_PASSWORD": "pw",
        **variables,
    }
    environ = {f"DB_LAKE_{name}": value for name, value in settings.items() if value is not None}
    return DataSource(name="lake", dialect="duckdb", prefix="DB_LAKE_", environ=environ)


# ---------------------------------------------------------------------------
# A stand-in for the driver and its workers
# ---------------------------------------------------------------------------


class DriverError(Exception):
    """Shaped like the driver's errors: a message, and the Chargewire code in ``details``."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.details = [(b"chargewire.code", code.encode())] if code else []


class FakeWorker:
    def __init__(self, name: str) -> None:
        self.name = name
        self.down = False
        self.serving = True
        self.queue_full = 0  # statements refused with rate_limited, then served
        self.draining = False  # refuses new sessions and statements, as on SIGTERM
        self.hang = False  # statements wait until released, or their cursor closes
        self.release = threading.Event()
        self.sessions = 0  # opened
        self.live: set[int] = set()  # session ids the worker still has
        self.statements: list[str] = []
        self.options: list[dict[str, str]] = []

    def restart(self) -> None:
        self.live.clear()


class FakeDriver:
    def __init__(self, workers: dict[str, FakeWorker]) -> None:
        self.workers = workers

    def connect(self, *, uri: str, username: str, password: str, **_: Any) -> FakeConnection:
        worker = self.workers[uri]
        if worker.down:
            raise DriverError(f"IO: [chargewire] connecting to {uri}: dial tcp: connection refused")
        if worker.draining:
            raise DriverError("IO: the server is shutting down", "server_draining")
        worker.sessions += 1
        worker.live.add(worker.sessions)
        return FakeConnection(worker, worker.sessions)


class FakeConnection:
    def __init__(self, worker: FakeWorker, session: int) -> None:
        self.worker = worker
        self.session = session
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class _Options:
    def __init__(self, worker: FakeWorker) -> None:
        self.worker = worker

    def set_options(self, **options: str) -> None:
        self.worker.options.append(options)


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.adbc_statement = _Options(conn.worker)
        self.closed = threading.Event()
        self.table: Any = None

    def execute(self, sql: str) -> None:
        worker = self.conn.worker
        if worker.down:
            raise DriverError("IO: [chargewire] ExecuteQuery: dial tcp: connection refused")
        if self.conn.session not in worker.live:
            raise DriverError("IO: the server no longer has session", "session_not_found")
        if worker.queue_full:
            worker.queue_full -= 1
            raise DriverError("IO: the statement queue is full", "rate_limited")
        if worker.draining:
            raise DriverError("IO: the server is shutting down", "server_draining")
        if worker.hang:
            deadline = time.monotonic() + 10
            while not (self.closed.is_set() or worker.release.is_set()):
                if time.monotonic() > deadline:
                    raise AssertionError("a hung statement was never ended nor released")
                time.sleep(0.005)
            if self.closed.is_set():
                raise DriverError("CANCELLED: the statement was cancelled")
        if sql.startswith("BAD"):
            raise DriverError("INVALID_ARGUMENT: Parser Error", "sql_error")
        worker.statements.append(sql)
        if sql.upper().startswith("SET"):
            self.table = None
        elif "current_setting" in sql:
            self.table = pa.table({"tz": ["Europe/Berlin"]})
        else:
            self.table = pa.table({"worker": [worker.name], "n": [len(worker.statements)]})

    @property
    def description(self) -> Any:
        if self.table is None:
            return None
        return [(name, None) for name in self.table.column_names]

    def fetch_arrow_table(self) -> Any:
        return self.table

    def fetch_record_batch(self) -> Any:
        return self.table.to_reader()

    def fetchone(self) -> Any:
        return tuple(self.table.to_pylist()[0].values())

    def fetchall(self) -> Any:
        return [tuple(row.values()) for row in self.table.to_pylist()]

    def close(self) -> None:
        self.closed.set()


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, FakeWorker]]:
    """The two workers of ``lake``, played by the stand-in; health from ``serving``."""
    found = {W1: FakeWorker("w1"), W2: FakeWorker("w2")}
    by_url = {
        "http://w1:31337/chargewire/v1/health": found[W1],
        "http://w2:31337/chargewire/v1/health": found[W2],
    }
    monkeypatch.setattr(duckdb_pool, "_driver", lambda source: FakeDriver(found))

    def health(self: Any, worker: Any) -> str:
        played = by_url[worker.health_url]
        if played.down:
            return duckdb_pool.SILENT
        return duckdb_pool.SERVING if played.serving else duckdb_pool.NOT_SERVING

    monkeypatch.setattr(duckdb_pool.Pool, "_health", health)
    monkeypatch.setattr(duckdb_pool, "WATCH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(duckdb_pool, "WATCHDOG_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(duckdb_pool, "RATE_LIMITED_PAUSE_SECONDS", 0.0)
    yield found
    duckdb_pool.close_pools()


def run(source: DataSource, sql: str = "SELECT 1") -> list[list[Any]]:
    return execute_sql(sql, dialect="duckdb", source=source).rows


def _in_thread(call: Any) -> Any:
    """Run ``call`` in a thread; the returned function waits and returns, or raises."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()

    def outcome() -> Any:
        thread.join(5)
        assert not thread.is_alive(), "the query never ended"
        if "error" in box:
            raise box["error"]
        return box["value"]

    return outcome


def wait_until(condition: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


class TestOffByDefault:
    def test_a_source_without_the_setting_runs_in_process(self) -> None:
        source = DataSource(name="lake", dialect="duckdb", prefix="DB_LAKE_", environ={})
        assert duckdb_pool.backend(source) == duckdb_pool.EMBEDDED
        assert duckdb_pool.pooled(source) is None
        assert duckdb_pool.pooled("duckdb") is None

    def test_a_query_in_process_imports_no_driver_and_starts_no_pool(self) -> None:
        # A process of its own: the driver is installed here, and other tests import it.
        code = (
            "import sys\n"
            "from ob_flight.datasources import DataSource\n"
            "from orionbelt.service import db_executor, duckdb_pool\n"
            "src = DataSource(name='lake', dialect='duckdb', prefix='DB_LAKE_',"
            " environ={'DB_LAKE_DUCKDB_DATABASE': ':memory:'})\n"
            "rows = db_executor.execute_sql('SELECT 42', dialect='duckdb', source=src).rows\n"
            "assert rows == [[42]]\n"
            "assert 'adbc_driver_hardcharger' not in sys.modules, 'driver imported'\n"
            "assert not duckdb_pool._POOLS, 'pool created'\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert result.stdout.strip() == "ok", result.stderr[-2000:]

    def test_the_unprefixed_setting_does_not_move_a_named_source(self) -> None:
        source = DataSource(
            name="lake",
            dialect="duckdb",
            prefix="DB_LAKE_",
            environ={"DUCKDB_BACKEND": "hardcharger"},
        )
        assert duckdb_pool.pooled(source) is None


# ---------------------------------------------------------------------------
# Settings, checked before the first query
# ---------------------------------------------------------------------------


class TestSettings:
    def test_the_settings_and_their_defaults(self) -> None:
        settings = duckdb_pool.pool_settings(lake(DUCKDB_CONNECTION_SQL="SET search_path = 'x'"))
        assert [w.name for w in settings.workers] == ["w1:31337", "w2:31337"]
        assert settings.workers[0].health_url == "http://w1:31337/chargewire/v1/health"
        assert settings.connection_sql == "SET search_path = 'x'"
        assert settings.connections_per_worker == 4
        assert settings.query_timeout_seconds == 120
        assert "pw" not in repr(settings)

    def test_tls_is_on_unless_turned_off(self) -> None:
        settings = duckdb_pool.pool_settings(
            lake(HARDCHARGER_WORKERS="hardcharger://node2:4000,hardcharger://[::1]")
        )
        assert [w.health_url for w in settings.workers] == [
            "https://node2:4000/chargewire/v1/health",
            "https://[::1]:31337/chargewire/v1/health",
        ]

    @pytest.mark.parametrize(
        ("variables", "names"),
        [
            ({"DUCKDB_BACKEND": "remote"}, "DB_LAKE_DUCKDB_BACKEND"),
            ({"HARDCHARGER_WORKERS": " , "}, "DB_LAKE_HARDCHARGER_WORKERS"),
            ({"HARDCHARGER_WORKERS": "http://w1:31337"}, "hardcharger://host"),
            ({"HARDCHARGER_WORKERS": "hardcharger://w1:0"}, "hardcharger://host"),
            ({"HARDCHARGER_WORKERS": f"{W1},{W1}"}, "twice"),
            ({"HARDCHARGER_USER": ""}, "DB_LAKE_HARDCHARGER_USER"),
            ({"HARDCHARGER_PASSWORD": None}, "DB_LAKE_HARDCHARGER_PASSWORD"),
            ({"HARDCHARGER_CONNECTIONS_PER_WORKER": "0"}, "CONNECTIONS_PER_WORKER"),
            ({"HARDCHARGER_QUERY_TIMEOUT_SECONDS": "soon"}, "QUERY_TIMEOUT_SECONDS"),
            ({"HARDCHARGER_TLS_ROOT_CERTS": "/no/such/ca.pem"}, "not a file"),
        ],
    )
    def test_an_unusable_setting_is_named(self, variables: dict[str, Any], names: str) -> None:
        source = lake(**variables)
        with pytest.raises(PoolConfigError, match=names):
            duckdb_pool.check_sources([source])

    @pytest.mark.parametrize(
        "variable",
        ["DUCKDB_INIT_SQL", "DUCKDB_ALLOWED_PATHS", "DUCKDB_MEMORY_LIMIT", "DUCKDB_DATABASE"],
    )
    def test_a_setting_for_duckdb_in_process_is_refused_not_ignored(self, variable: str) -> None:
        with pytest.raises(PoolConfigError, match=f"DB_LAKE_{variable} does not apply"):
            duckdb_pool.pool_settings(lake(**{variable: "x"}))

    def test_the_implicit_source_cannot_run_on_the_workers(self) -> None:
        source = DataSource(
            name="duckdb", dialect="duckdb", environ={"DUCKDB_BACKEND": "hardcharger"}
        )
        with pytest.raises(PoolConfigError, match="implicit DuckDB source"):
            duckdb_pool.check_sources([source])

    def test_the_driver_missing_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "adbc_driver_hardcharger.dbapi", None)
        with pytest.raises(PoolConfigError, match=r"orionbelt-semantic-layer\[hardcharger\]"):
            duckdb_pool.check_sources([lake()])

    def test_other_sources_are_not_looked_at(self) -> None:
        other = DataSource(name="pg", dialect="postgres", prefix="DB_PG_", environ={})
        embedded = DataSource(name="files", dialect="duckdb", prefix="DB_FILES_", environ={})
        duckdb_pool.check_sources([other, embedded])

    def test_the_server_does_not_start_with_such_a_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OBSL_DATABASES", "lake")
        monkeypatch.setenv("DB_LAKE_TYPE", "duckdb")
        monkeypatch.setenv("DB_LAKE_DUCKDB_BACKEND", "hardcharger")
        monkeypatch.delenv("DB_LAKE_HARDCHARGER_WORKERS", raising=False)
        with pytest.raises(RuntimeError, match="DB_LAKE_HARDCHARGER_WORKERS"):
            api_app._check_duckdb_workers()


# ---------------------------------------------------------------------------
# Picking a worker
# ---------------------------------------------------------------------------


class TestPicking:
    def test_queries_take_the_workers_in_turn(self, workers: dict[str, FakeWorker]) -> None:
        source = lake()
        names = [run(source)[0][0] for _ in range(4)]
        assert sorted(names) == ["w1", "w1", "w2", "w2"]

    def test_a_held_connection_sends_the_next_query_to_the_other_worker(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        source = lake(HARDCHARGER_CONNECTIONS_PER_WORKER="1")
        held = duckdb_pool.cursor(source)
        held.execute("SELECT 1")
        busy = held.fetch_arrow_table().column("worker")[0].as_py()
        assert run(source)[0][0] != busy
        held.close()

    def test_no_connection_free_for_the_whole_wait(self, workers: dict[str, FakeWorker]) -> None:
        source = lake(HARDCHARGER_CONNECTIONS_PER_WORKER="1", ACQUIRE_TIMEOUT_SECONDS="0.2")
        held = [duckdb_pool.cursor(source), duckdb_pool.cursor(source)]
        for cursor in held:
            cursor.execute("SELECT 1")
        with pytest.raises(ExecutionUnavailableError, match=r"\[DATA_SOURCE_BUSY\]"):
            run(source)
        for cursor in held:
            cursor.close()
        assert run(source)

    def test_each_statement_carries_the_timeout(self, workers: dict[str, FakeWorker]) -> None:
        run(lake(HARDCHARGER_QUERY_TIMEOUT_SECONDS="30"))
        options = workers[W1].options + workers[W2].options
        assert {"adbc.chargewire.statement.timeout_seconds": "30"} in options


# ---------------------------------------------------------------------------
# Retrying what never ran
# ---------------------------------------------------------------------------


class TestRetries:
    def test_an_unreachable_worker_is_taken_out_and_the_other_answers(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        workers[W1].down = True
        source = lake()
        assert [run(source)[0][0] for _ in range(3)] == ["w2", "w2", "w2"]
        pool = duckdb_pool.pool(source)
        assert [s.healthy for s in pool._workers] == [False, True]

    def test_none_reachable(self, workers: dict[str, FakeWorker]) -> None:
        workers[W1].down = workers[W2].down = True
        with pytest.raises(ExecutionUnavailableError, match=r"\[WORKERS_UNAVAILABLE\]"):
            run(lake())

    def test_a_lost_session_is_replaced_and_its_statements_run_again(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        source = lake(
            HARDCHARGER_WORKERS=W1, DUCKDB_CONNECTION_SQL="SET search_path = 'lake.sales'"
        )
        run(source)
        workers[W1].restart()
        assert run(source, "SELECT 2")[0][0] == "w1"
        # Each session ran the connection statement before its first query
        # (the session time zone probe aside).
        queries = [sql for sql in workers[W1].statements if "current_setting" not in sql]
        assert queries == [
            "SET search_path = 'lake.sales'",
            "SELECT 1",
            "SET search_path = 'lake.sales'",
            "SELECT 2",
        ]

    def test_a_full_queue_is_retried(self, workers: dict[str, FakeWorker]) -> None:
        workers[W1].queue_full = workers[W2].queue_full = 2
        assert run(lake())

    def test_a_queue_full_for_the_whole_wait(self, workers: dict[str, FakeWorker]) -> None:
        workers[W1].queue_full = workers[W2].queue_full = 10**9
        with pytest.raises(ExecutionUnavailableError, match=r"\[DATA_SOURCE_BUSY\]"):
            run(lake(ACQUIRE_TIMEOUT_SECONDS="0.2"))

    def test_the_querys_own_failure_is_not_run_again(self, workers: dict[str, FakeWorker]) -> None:
        source = lake(HARDCHARGER_WORKERS=W1)
        with pytest.raises(ExecutionError, match="Parser Error"):
            run(source, "BAD SQL")
        assert workers[W1].statements == []
        # The session stays good for the next query.
        run(source)
        assert workers[W1].sessions == 1

    def test_failing_connection_statements_are_not_shown(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        source = lake(DUCKDB_CONNECTION_SQL="BAD SET secret_value")
        with pytest.raises(
            ExecutionUnavailableError, match="DUCKDB_CONNECTION_SQL failed"
        ) as caught:
            run(source)
        assert "secret_value" not in str(caught.value)


# ---------------------------------------------------------------------------
# Health and deadlines
# ---------------------------------------------------------------------------


class TestHealth:
    def test_a_worker_failing_its_health_check_takes_no_queries_until_it_answers(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        source = lake(HARDCHARGER_HEALTH_INTERVAL_SECONDS="0.05")
        pool = duckdb_pool.pool(source)
        workers[W2].serving = False
        wait_until(lambda: not pool._workers[1].healthy)
        assert {run(source)[0][0] for _ in range(4)} == {"w1"}
        workers[W2].serving = True
        wait_until(lambda: pool._workers[1].healthy)
        assert "w2" in {run(source)[0][0] for _ in range(4)}

    def test_a_statement_on_a_worker_that_stops_answering_is_ended(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        workers[W1].hang = True
        source = lake(HARDCHARGER_WORKERS=W1, HARDCHARGER_QUERY_TIMEOUT_SECONDS="0.2")
        started = time.monotonic()
        with pytest.raises(ExecutionError, match=r"\[WORKER_UNRESPONSIVE\].*did not answer"):
            run(source)
        assert time.monotonic() - started < 3
        assert duckdb_pool.pool(source)._workers[0].healthy is False

    def test_a_draining_worker_finishes_what_runs_on_it(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        workers[W1].hang = True
        source = lake(HARDCHARGER_WORKERS=W1, HARDCHARGER_HEALTH_INTERVAL_SECONDS="0.05")
        outcome = _in_thread(lambda: run(source))
        time.sleep(0.1)
        workers[W1].serving = False  # draining: it answers, 'not_serving'
        pool = duckdb_pool.pool(source)
        wait_until(lambda: not pool._workers[0].healthy)
        time.sleep(0.3)  # several health checks
        workers[W1].release.set()
        assert outcome() == [["w1", 1]]

    def test_the_statements_of_a_worker_silent_twice_in_a_row_are_ended(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        workers[W1].hang = True
        source = lake(HARDCHARGER_WORKERS=W1, HARDCHARGER_HEALTH_INTERVAL_SECONDS="0.05")
        outcome = _in_thread(lambda: run(source))
        time.sleep(0.1)
        workers[W1].down = True  # no answer at all
        with pytest.raises(ExecutionError, match="stopped answering its health checks"):
            outcome()

    def test_a_worker_shutting_down_is_taken_out_and_the_query_runs_elsewhere(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        source = lake()
        run(source)
        run(source)  # both workers hold an idle session now
        workers[W1].draining = True
        assert {run(source)[0][0] for _ in range(3)} == {"w2"}
        assert duckdb_pool.pool(source)._workers[0].healthy is False

    def test_a_read_after_the_pool_ended_it_says_so(self, workers: dict[str, FakeWorker]) -> None:
        pool = duckdb_pool.pool(lake(HARDCHARGER_WORKERS=W1))
        cursor = pool.cursor()
        cursor.execute("SELECT 1")
        assert cursor._lease is not None
        pool._end(cursor._lease._statement, "a test ended it")
        cursor._cursor.fetch_arrow_table = lambda: (_ for _ in ()).throw(OSError("closed"))
        with pytest.raises(WorkerTimeoutError, match="a test ended it"):
            cursor.fetch_arrow_table()
        cursor.close()


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------


class TestSurfaces:
    def test_explain_runs_on_the_workers(self, workers: dict[str, FakeWorker]) -> None:
        explain_sql("SELECT 1", dialect="duckdb", source=lake(HARDCHARGER_WORKERS=W1))
        assert workers[W1].statements == ["EXPLAIN SELECT 1"]

    def test_the_session_time_zone_is_read_on_the_workers(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        db_executor._reset_db_tz_cache()
        try:
            zone = db_executor.warm_db_tz_cache("duckdb", lake(HARDCHARGER_WORKERS=W1))
        finally:
            db_executor._reset_db_tz_cache()
        assert str(zone) == "Europe/Berlin"

    def test_flight_gets_a_connection_whose_cursors_run_on_the_workers(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        conn = db_router.connect(lake(HARDCHARGER_WORKERS=W1))
        assert isinstance(conn, duckdb_pool.PoolConnection)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetch_arrow_table().column("worker").to_pylist() == ["w1"]
        conn.close()
        pool = duckdb_pool.pool(lake(HARDCHARGER_WORKERS=W1))
        assert pool._workers[0].in_use == 0

    def test_a_result_streams_through_a_sink(self, workers: dict[str, FakeWorker]) -> None:
        seen: list[Any] = []

        def sink(reader: Any) -> Any:
            seen.append(reader)
            return reader.read_all()

        result = execute_sql("SELECT 1", dialect="duckdb", source=lake(), sink=sink)
        assert seen and result.row_count == 1

    def test_workers_unavailable_is_a_503_not_a_database_failure(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        workers[W1].down = workers[W2].down = True
        with pytest.raises(ExecutionUnavailableError) as caught:
            run(lake())
        assert isinstance(caught.value, WorkersUnavailableError)
        assert not isinstance(caught.value, ExecutionError)

    def test_changed_settings_get_a_new_pool_and_the_old_one_closes(
        self, workers: dict[str, FakeWorker]
    ) -> None:
        old = duckdb_pool.pool(lake())
        assert duckdb_pool.pool(lake()) is old
        new = duckdb_pool.pool(lake(HARDCHARGER_CONNECTIONS_PER_WORKER="2"))
        assert new is not old
        old._thread.join(2)
        assert not old._thread.is_alive() and new._thread.is_alive()

    def test_closing_the_pools_stops_their_threads(self, workers: dict[str, FakeWorker]) -> None:
        pool = duckdb_pool.pool(lake())
        duckdb_pool.close_pools()
        pool._thread.join(2)
        assert not pool._thread.is_alive()
