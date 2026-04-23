"""Integration tests: ob-mysql PEP 249 driver against a real MySQL via testcontainers.

Tests the full driver path: OBML YAML → detect → compile → execute → fetch.
The REST API compilation is replaced with direct Python compilation to avoid
needing a running API server.

    uv run pytest -m docker

Skipped automatically when:
- ob-mysql, testcontainers, or mysql-connector-python packages are not installed
- Docker is not running
"""

from __future__ import annotations

from typing import Any

import pytest

# Skip entire module if dependencies are missing
pytest.importorskip("testcontainers.mysql", reason="testcontainers[mysql] required")
ob_mysql = pytest.importorskip("ob_mysql", reason="ob-mysql driver required")
pytest.importorskip("pyarrow", reason="pyarrow required")

import pyarrow as pa  # noqa: E402
from testcontainers.mysql import MySqlContainer  # noqa: E402

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402
from tests.conftest import SALES_MODEL_DIR  # noqa: E402

pytestmark = pytest.mark.docker

# ---------------------------------------------------------------------------
# Test data — same as test_mysql_execution.py
# ---------------------------------------------------------------------------

_SETUP_SQL = """\
CREATE DATABASE IF NOT EXISTS `PUBLIC`;

CREATE TABLE `PUBLIC`.`CUSTOMERS` (
    `CUSTOMER_ID` VARCHAR(255), `NAME` VARCHAR(255),
    `COUNTRY` VARCHAR(255), `SEGMENT` VARCHAR(255)
);
INSERT INTO `PUBLIC`.`CUSTOMERS` VALUES
    ('C1', 'Alice',   'US', 'SMB'),
    ('C2', 'Bob',     'UK', 'Enterprise'),
    ('C3', 'Charlie', 'US', 'MidMarket');

CREATE TABLE `PUBLIC`.`PRODUCTS` (
    `PRODUCT_ID` VARCHAR(255), `NAME` VARCHAR(255), `CATEGORY` VARCHAR(255)
);
INSERT INTO `PUBLIC`.`PRODUCTS` VALUES
    ('P1', 'Widget', 'Hardware'),
    ('P2', 'Gadget', 'Software');

CREATE TABLE `PUBLIC`.`ORDERS` (
    `ORDER_ID` VARCHAR(255), `ORDER_DATE` DATE, `CUSTOMER_ID` VARCHAR(255),
    `PRODUCT_ID` VARCHAR(255), `QUANTITY` INT, `PRICE` DOUBLE
);
INSERT INTO `PUBLIC`.`ORDERS` VALUES
    ('O1', '2024-01-15', 'C1', 'P1', 10,  5.0),
    ('O2', '2024-01-20', 'C1', 'P2',  2, 25.0),
    ('O3', '2024-02-10', 'C2', 'P1',  5,  5.0),
    ('O4', '2024-02-15', 'C3', 'P2',  1, 100.0),
    ('O5', '2024-03-01', 'C2', 'P1',  3,  5.0);
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def sales_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


@pytest.fixture(scope="module")
def _patch_compiler(sales_model: SemanticModel) -> Any:
    """Replace REST-based compile_obml with direct Python compilation."""
    import ob_mysql.cursor as cursor_mod

    pipeline = CompilationPipeline()
    original = cursor_mod.compile_obml

    def direct_compile(
        obml: dict[str, Any],
        *,
        dialect: str,
        ob_api_url: str = "",
        ob_timeout: int = 30,
    ) -> str:
        query = QueryObject.model_validate(obml)
        return pipeline.compile(query, sales_model, dialect).sql

    cursor_mod.compile_obml = direct_compile  # type: ignore[assignment]
    yield
    cursor_mod.compile_obml = original  # type: ignore[assignment]


@pytest.fixture(scope="module")
def mysql_conn(_patch_compiler: Any):
    """Spin up MySQL, seed data, return ob-mysql driver connection."""
    if not _docker_available():
        pytest.skip("Docker is not running")

    import mysql.connector

    with MySqlContainer("mysql:8.0") as mysql_c:
        # Seed data using mysql-connector as root
        raw = mysql.connector.connect(
            host=mysql_c.get_container_host_ip(),
            port=int(mysql_c.get_exposed_port(3306)),
            user="root",
            password=mysql_c.root_password,
            autocommit=True,
        )
        cur = raw.cursor()
        for stmt in _SETUP_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        cur.close()
        raw.close()

        # Connect via ob-mysql driver
        conn = ob_mysql.connect(
            host=mysql_c.get_container_host_ip(),
            port=int(mysql_c.get_exposed_port(3306)),
            user="root",
            password=mysql_c.root_password,
            database="PUBLIC",
        )
        yield conn
        conn.close()


# ---------------------------------------------------------------------------
# OBML query execution via driver
# ---------------------------------------------------------------------------

_REVENUE_BY_COUNTRY = """\
select:
  dimensions:
    - Customer Country
  measures:
    - Revenue
"""

_MULTI_MEASURE = """\
select:
  dimensions:
    - Customer Country
  measures:
    - Revenue
    - Order Count
"""

_REVENUE_SHARE = """\
select:
  dimensions:
    - Customer Country
  measures:
    - Revenue Share
"""


def _rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]


class TestOBMySQLDriver:
    """Execute OBML YAML queries through the ob-mysql PEP 249 driver."""

    def test_obml_revenue_by_country(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_BY_COUNTRY)
        rows = _rows_to_dicts(cur)
        cur.close()

        by_country = {r["Customer Country"]: r["Revenue"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0)
        assert float(by_country["UK"]) == pytest.approx(40.0)

    def test_obml_multi_measure(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_MULTI_MEASURE)
        rows = _rows_to_dicts(cur)
        cur.close()

        by_country = {r["Customer Country"]: r for r in rows}
        assert float(by_country["US"]["Revenue"]) == pytest.approx(200.0)
        assert by_country["US"]["Order Count"] == 3

    def test_obml_derived_metric(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_SHARE)
        rows = _rows_to_dicts(cur)
        cur.close()

        by_country = {r["Customer Country"]: r["Revenue Share"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 240.0, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(40.0 / 240.0, rel=1e-3)

    def test_plain_sql_passthrough(self, mysql_conn) -> None:
        """Plain SQL bypasses OBML compilation."""
        cur = mysql_conn.cursor()
        cur.execute("SELECT 1 AS n")
        rows = cur.fetchall()
        cur.close()
        assert rows == [(1,)]

    def test_fetch_arrow_table(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_BY_COUNTRY)
        table = cur.fetch_arrow_table()
        cur.close()

        assert isinstance(table, pa.Table)
        assert table.num_rows == 2
        assert "Customer Country" in table.column_names
        assert "Revenue" in table.column_names

    def test_cursor_description(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_BY_COUNTRY)
        assert cur.description is not None
        col_names = [d[0] for d in cur.description]
        assert "Customer Country" in col_names
        assert "Revenue" in col_names
        cur.fetchall()  # consume results before close (MySQL requires this)
        cur.close()
