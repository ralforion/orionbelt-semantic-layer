"""An Iceberg catalog served over REST, read through a DuckDB data source.

Starts MinIO and Apache Iceberg's REST catalog fixture, writes tables of two
namespaces through DuckDB, and runs a model over them on a source with no
database file, whose setup statements (``DUCKDB_INIT_SQL``) create the S3
secret and attach the catalog.

Opt-in - requires Docker, and network access for DuckDB's iceberg extension::

    uv run pytest -m docker tests/integration/test_iceberg_rest_source.py
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("testcontainers.core.container", reason="testcontainers required")
pytest.importorskip("ob_flight")
duckdb = pytest.importorskip("duckdb")

from ob_flight import db_router  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.network import Network  # noqa: E402

from orionbelt.cli import _local  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service import duckdb_source  # noqa: E402

pytestmark = pytest.mark.docker

KEY_ID, SECRET = "admin", "password"


@dataclass(frozen=True)
class Catalog:
    rest: str  # http://host:port of the REST catalog
    s3: str  # host:port of MinIO

    def setup_sql(self, *, search_path: bool = True) -> str:
        """The source's setup statements: S3 secret, the catalog, its namespace."""
        statements = [
            f"CREATE OR REPLACE SECRET lake_s3 (TYPE s3, KEY_ID '{KEY_ID}', SECRET '{SECRET}', "
            f"REGION 'us-east-1', ENDPOINT '{self.s3}', URL_STYLE 'path', USE_SSL false)",
            f"ATTACH IF NOT EXISTS 'warehouse' AS lake (TYPE iceberg, ENDPOINT '{self.rest}', "
            "AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none')",
        ]
        if search_path:
            # The DuckDB dialect names a table schema.table; the search path
            # finds the schema (an Iceberg namespace) in the attached catalog.
            statements.append("SET search_path = 'lake.sales'")
        return "; ".join(statements)


def _wait_until_up(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="module")
def catalog() -> Iterator[Catalog]:
    with Network() as network:
        minio = (
            DockerContainer("minio/minio:latest")
            .with_network(network)
            .with_network_aliases("minio")
            .with_env("MINIO_ROOT_USER", KEY_ID)
            .with_env("MINIO_ROOT_PASSWORD", SECRET)
            .with_exposed_ports(9000)
            .with_command("server /data")
        )
        rest = (
            DockerContainer("apache/iceberg-rest-fixture:latest")
            .with_network(network)
            .with_env("AWS_ACCESS_KEY_ID", KEY_ID)
            .with_env("AWS_SECRET_ACCESS_KEY", SECRET)
            .with_env("AWS_REGION", "us-east-1")
            .with_env("CATALOG_WAREHOUSE", "s3://warehouse/")
            .with_env("CATALOG_IO__IMPL", "org.apache.iceberg.aws.s3.S3FileIO")
            .with_env("CATALOG_S3_ENDPOINT", "http://minio:9000")
            .with_env("CATALOG_S3_PATH__STYLE__ACCESS", "true")
            .with_exposed_ports(8181)
        )
        with minio, rest:
            s3 = f"{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"
            _wait_until_up(f"http://{s3}/minio/health/live")
            created = minio.exec(
                [
                    "sh",
                    "-c",
                    f"mc alias set m http://127.0.0.1:9000 {KEY_ID} {SECRET} && mc mb m/warehouse",
                ]
            )
            assert created.exit_code == 0, created.output
            url = f"http://{rest.get_container_host_ip()}:{rest.get_exposed_port(8181)}"
            _wait_until_up(f"{url}/v1/config")
            found = Catalog(rest=url, s3=s3)
            _write(found, ["CREATE SCHEMA lake.sales", "CREATE SCHEMA lake.finance"])
            _write(
                found,
                [
                    "CREATE TABLE lake.sales.orders AS SELECT i AS id, 'c' || (i % 2) AS cust, "
                    "(i * 2.0)::DOUBLE AS amount FROM range(10) t(i)",
                    "CREATE TABLE lake.finance.budget AS "
                    "SELECT * FROM (VALUES ('c0', 100.0::DOUBLE)) t(cust, budget)",
                ],
            )
            yield found


def _write(catalog: Catalog, statements: list[str]) -> None:
    """Run statements through a writer of its own, as another client would."""
    con = duckdb.connect()
    try:
        try:
            con.execute(catalog.setup_sql(search_path=False))
        except duckdb.IOException as exc:
            pytest.skip(f"DuckDB's iceberg extension is not available: {exc}")
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()


@pytest.fixture
def lake(catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> Iterator[Catalog]:
    """The named source ``lake``: no database file, the catalog attached."""
    for variable in ("DUCKDB_DATABASE", "DUCKDB_DATABASE_PATH", "DUCKDB_INIT_SQL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OBSL_DATABASES", "lake")
    monkeypatch.setenv("DB_LAKE_TYPE", "duckdb")
    monkeypatch.setenv("DB_LAKE_DUCKDB_INIT_SQL", catalog.setup_sql())
    yield catalog
    duckdb_source.close_instances()


MODEL = """\
version: 1.0
settings:
  defaultDialect: duckdb
  defaultDataSource: lake
dataObjects:
  Orders:
    code: orders
    schema: sales
    columns:
      Order Id:
        code: id
        abstractType: int
        primaryKey: true
      Order Customer:
        code: cust
        abstractType: string
      Amount:
        code: amount
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Budgets
        columnsFrom: [Order Customer]
        columnsTo: [Budget Customer]
  Budgets:
    code: budget
    schema: finance
    columns:
      Budget Customer:
        code: cust
        abstractType: string
        primaryKey: true
      Budget:
        code: budget
        abstractType: float
dimensions:
  Customer:
    dataObject: Orders
    column: Order Customer
    resultType: string
  Budget:
    dataObject: Budgets
    column: Budget
    resultType: float
measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""

BY_CUSTOMER = QueryObject.model_validate(
    {
        "select": {"dimensions": ["Customer", "Budget"], "measures": ["Revenue"]},
        "orderBy": [{"field": "Customer"}],
    }
)


def _rows() -> list[list[Any]]:
    return _local.execute_query(MODEL, BY_CUSTOMER, None)[1].rows


class TestIcebergRestSource:
    def test_a_model_joins_tables_of_two_namespaces(self, lake: Catalog) -> None:
        assert _rows()[:2] == [["c0", 100.0, 40.0], ["c1", None, 50.0]]

    def test_a_commit_is_read_by_the_next_query(self, lake: Catalog) -> None:
        """The source keeps its instance, with the catalog attached; a table's
        newest snapshot is still what each query reads."""
        before = _rows()
        _write(lake, ["INSERT INTO lake.sales.orders VALUES (100, 'c9', 1.0)"])
        try:
            assert _rows() == [*before, ["c9", None, 1.0]]
        finally:
            _write(lake, ["DELETE FROM lake.sales.orders WHERE id = 100"])

    def test_flight_reads_it_too(self, lake: Catalog) -> None:
        conn = db_router.connect(_lake_source())
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT count(*) FROM "sales"."orders"')
            assert cursor.fetchall() == [(10,)]
        finally:
            conn.close()

    def test_online_validation_reads_the_catalogs_tables(self, lake: Catalog) -> None:
        assert _local.validate(MODEL, online=True).errors == []

        column = "code: budget\n        abstractType"
        drifted = MODEL.replace(column, column.replace("budget", "budget_eur"))
        codes = [e.code for e in _local.validate(drifted, online=True).errors]

        assert codes == ["DATASOURCE_COLUMN_MISSING"]


def _lake_source() -> Any:
    from ob_flight.datasources import configured_datasources

    return next(source for source in configured_datasources() if source.name == "lake")
