"""A DuckDB source on real Hardcharger workers, through the real driver.

Two workers from the pinned image, each with the init SQL that creates the
source's user and grants it ``/data``. The same Parquet file is read in
process and on the workers, and the results must agree. ``pytest -m docker``.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("ob_flight")
pytest.importorskip("adbc_driver_hardcharger")
duckdb = pytest.importorskip("duckdb")
pytest.importorskip("testcontainers")

from ob_flight import db_router  # noqa: E402
from ob_flight.datasources import DataSource  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402

from orionbelt.service import db_executor, duckdb_pool, duckdb_source  # noqa: E402
from orionbelt.service.db_executor import ExecutionError, execute_sql, explain_sql  # noqa: E402

pytestmark = pytest.mark.docker

#: The release the pool was tested with (deploy/compose/compose.pool.yml pins the same).
IMAGE = (
    "ghcr.io/gizmodata/hardcharger:2.12.1"
    "@sha256:bc189d35619f6dcf21f9bbd6b089e3039e5270a9a324184acdd5bcde8dfba086"
)
PASSWORD = "integration-pw"
INIT_SQL = f"""\
CREATE ROLE IF NOT EXISTS lake_reader;
GRANT READ FILESYSTEM ON LOCATION '/data/' TO lake_reader;
CREATE USER IF NOT EXISTS obsl_lake WITH PASSWORD '{PASSWORD}';
GRANT lake_reader TO obsl_lake;
"""
#: One column of each kind a model reads.
COLUMNS = """
    i AS id,
    'c' || (i % 3) AS cust,
    (i * 1.5)::DOUBLE AS amount,
    (i * 1.25)::DECIMAL(18, 2) AS price,
    DATE '2026-01-01' + i::INTEGER AS day,
    TIMESTAMP '2026-01-01 08:00:00' + to_hours(i) AS at,
    TIMESTAMPTZ '2026-01-01 08:00:00+00' + to_hours(i) AS at_tz,
    i % 2 = 0 AS even,
    [i, i + 1] AS pair
"""


def _serving(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - http, local
            return bool(json.loads(response.read()).get("status") == "serving")
    except OSError:
        return False


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("hardcharger")
    (folder / "data").mkdir()
    duckdb.sql(
        f"COPY (SELECT {COLUMNS} FROM range(30) t(i)) "
        f"TO '{folder / 'data' / 'orders.parquet'}' (FORMAT parquet)"
    )
    (folder / "init.sql").write_text(INIT_SQL)
    for path in (folder, folder / "data", folder / "init.sql", folder / "data" / "orders.parquet"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return folder


@pytest.fixture(scope="module")
def workers(data: Path) -> Iterator[list[DockerContainer]]:
    """Two workers; each evicts a session idle for 2 s (within 5 s more)."""
    started = []
    try:
        for _ in range(2):
            worker = (
                DockerContainer(IMAGE)
                .with_env("HARDCHARGER_ADMIN_PASSWORD", "integration-admin")
                .with_env("HARDCHARGER_SECRET_KEY", "0123456789abcdef0123456789abcdef")
                .with_env("HARDCHARGER_RBAC_STORE_URI", "duckdb:/var/lib/hardcharger/rbac.duckdb")
                .with_env("HARDCHARGER_INIT_SQL_COMMANDS_FILE", "/init/init.sql")
                .with_env("HARDCHARGER_SESSION_IDLE_TIMEOUT", "2")
                .with_volume_mapping(str(data), "/init", "ro")
                .with_volume_mapping(str(data / "data"), "/data", "ro")
                .with_exposed_ports(31337)
            )
            worker.start()
            started.append(worker)
        for worker in started:
            url = f"http://{_address(worker)}/chargewire/v1/health"
            deadline = time.monotonic() + 60
            while not _serving(url):
                if time.monotonic() > deadline:
                    pytest.fail(f"worker not serving: {worker.get_logs()}")
                time.sleep(0.5)
        yield started
    finally:
        for worker in started:
            worker.stop()


def _address(worker: DockerContainer) -> str:
    return f"{worker.get_container_host_ip()}:{worker.get_exposed_port(31337)}"


def on_workers(workers: list[DockerContainer], **variables: str) -> DataSource:
    settings = {
        "DUCKDB_BACKEND": "hardcharger",
        "HARDCHARGER_WORKERS": ",".join(f"hardcharger://{_address(w)}?tls=false" for w in workers),
        "HARDCHARGER_USER": "obsl_lake",
        "HARDCHARGER_PASSWORD": PASSWORD,
        **variables,
    }
    environ = {f"DB_LAKE_{name}": value for name, value in settings.items()}
    return DataSource(name="lake", dialect="duckdb", prefix="DB_LAKE_", environ=environ)


def in_process(data: Path) -> DataSource:
    environ = {
        "DB_FILES_DUCKDB_DATABASE": ":memory:",
        "DB_FILES_DUCKDB_ALLOWED_PATHS": f"{data / 'data'}/",
    }
    return DataSource(name="files", dialect="duckdb", prefix="DB_FILES_", environ=environ)


@pytest.fixture(autouse=True)
def _close() -> Iterator[None]:
    yield
    duckdb_pool.close_pools()
    duckdb_source.close_instances()


def query(directory: str) -> str:
    return f"SELECT * FROM read_parquet('{directory}/orders.parquet') ORDER BY id"


class TestOnWorkers:
    def test_a_result_is_the_same_as_in_process(
        self, workers: list[DockerContainer], data: Path
    ) -> None:
        remote = execute_sql(query("/data"), dialect="duckdb", source=on_workers(workers))
        local = execute_sql(query(str(data / "data")), dialect="duckdb", source=in_process(data))
        assert remote.arrow_table is not None and local.arrow_table is not None
        assert remote.arrow_table.schema == local.arrow_table.schema
        assert remote.arrow_table.equals(local.arrow_table)
        assert [c.type_hint for c in remote.columns] == [c.type_hint for c in local.columns]

    def test_both_workers_answer(self, workers: list[DockerContainer]) -> None:
        source = on_workers(workers, HARDCHARGER_CONNECTIONS_PER_WORKER="1")
        with duckdb_pool.cursor(source) as held:
            held.execute("SELECT 1")
            # The one connection of the held worker is in use: the other answers.
            assert execute_sql("SELECT 2 AS n", dialect="duckdb", source=source).rows == [[2]]
        assert [len(s.idle) for s in duckdb_pool.pool(source)._workers] == [1, 1]

    def test_a_session_the_worker_evicted_is_replaced_with_its_statements(
        self, workers: list[DockerContainer]
    ) -> None:
        source = on_workers(
            workers[:1],
            DUCKDB_CONNECTION_SQL="SET TimeZone = 'Asia/Tokyo'; SET search_path = 'main'",
        )
        zone = "SELECT current_setting('TimeZone') AS zone"
        assert execute_sql(zone, dialect="duckdb", source=source).rows == [["Asia/Tokyo"]]
        worker = duckdb_pool.pool(source)._workers[0]
        evicted = list(worker.idle)
        time.sleep(8)  # idle past the worker's 2 s, and its 5 s to evict
        assert execute_sql(zone, dialect="duckdb", source=source).rows == [["Asia/Tokyo"]]
        # The session was gone: the pool opened another and ran the statements on it.
        assert evicted and not any(conn in worker.idle for conn in evicted)

    def test_explain_and_the_session_time_zone(self, workers: list[DockerContainer]) -> None:
        source = on_workers(workers, DUCKDB_CONNECTION_SQL="SET TimeZone = 'Europe/Berlin'")
        plan = explain_sql(query("/data"), dialect="duckdb", source=source)
        assert "READ_PARQUET" in plan.upper() or "PARQUET" in plan.upper()
        db_executor._reset_db_tz_cache()
        try:
            assert str(db_executor.warm_db_tz_cache("duckdb", source)) == "Europe/Berlin"
        finally:
            db_executor._reset_db_tz_cache()

    def test_a_file_outside_the_grant_is_refused_naming_the_grants(
        self, workers: list[DockerContainer]
    ) -> None:
        with pytest.raises(ExecutionError, match="READ FILESYSTEM.*workers' init SQL"):
            execute_sql(
                "SELECT * FROM read_text('/etc/hostname')",
                dialect="duckdb",
                source=on_workers(workers),
            )

    def test_flight_reads_through_the_pool(self, workers: list[DockerContainer]) -> None:
        conn: Any = db_router.connect(on_workers(workers))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 42 AS answer")
            assert cursor.fetch_arrow_table().to_pylist() == [{"answer": 42}]
        finally:
            conn.close()
