"""Integration tests: compile queries and execute against a real MySQL via testcontainers.

These tests validate that the MySQL dialect produces correct, executable SQL
against a real MySQL database.  They are **opt-in** and require Docker:

    uv run pytest -m docker

Skipped automatically when:
- testcontainers or pymysql packages are not installed
- Docker is not running
"""

from __future__ import annotations

from typing import Any

import pytest

# Skip entire module if dependencies are missing
testcontainers_mysql = pytest.importorskip(
    "testcontainers.mysql", reason="testcontainers[mysql] required"
)
pymysql = pytest.importorskip("pymysql", reason="pymysql required")

from testcontainers.mysql import MySqlContainer  # noqa: E402

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import (  # noqa: E402
    FilterOperator,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402
from tests.conftest import SALES_MODEL_DIR  # noqa: E402

# Mark ALL tests in this module as docker (opt-in)
pytestmark = pytest.mark.docker

# ---------------------------------------------------------------------------
# Test data (same values as other execution tests for baseline comparison)
# ---------------------------------------------------------------------------

# MySQL uses backtick quoting.  The compiled SQL references tables as
# `PUBLIC`.`ORDERS` (backtick-quoted schema.table).  DDL must create a
# `PUBLIC` database with matching table and column names.
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

# Expected values (identical to DuckDB/Postgres baseline):
# Revenue by country:  US=200.0, UK=40.0
# Order count:         US=3, UK=2
# Grand Total Revenue: 240.0 (all rows)
# Revenue per Order:   US≈66.667, UK=20.0
# Revenue Share:       US≈0.833, UK≈0.167


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Check if Docker daemon is reachable."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def mysql_conn():
    """Spin up a MySQL container and return a pymysql connection.

    Skips if Docker is not running.
    """
    if not _docker_available():
        pytest.skip("Docker is not running")

    with MySqlContainer("mysql:8.0") as mysql:
        conn = pymysql.connect(
            host=mysql.get_container_host_ip(),
            port=int(mysql.get_exposed_port(3306)),
            user="root",
            password=mysql.root_password,
            autocommit=True,
        )
        cur = conn.cursor()
        for stmt in _SETUP_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        cur.close()
        yield conn
        conn.close()


@pytest.fixture(scope="module")
def sales_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


@pytest.fixture(scope="module")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


def _execute_dict(conn: Any, sql: str) -> list[dict[str, Any]]:
    """Execute SQL on the MySQL connection and return rows as dicts."""
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Star-schema queries
# ---------------------------------------------------------------------------


class TestMySQLStarSchema:
    """Compile with dialect=mysql and execute against real MySQL."""

    def test_revenue_by_country(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0)
        assert by_country["UK"] == pytest.approx(40.0)

    def test_order_count_by_country(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r["Order Count"] for r in rows}
        assert by_country["US"] == 3
        assert by_country["UK"] == 2

    def test_multi_measure(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Order Count"] == 3
        assert by_country["UK"]["Revenue"] == pytest.approx(40.0)
        assert by_country["UK"]["Order Count"] == 2

    def test_revenue_by_product_category(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Product Category"], measures=["Revenue"]),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_cat = {r["Product Category"]: r["Revenue"] for r in rows}
        assert by_cat["Hardware"] == pytest.approx(90.0)
        assert by_cat["Software"] == pytest.approx(150.0)

    def test_average_order_value(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Average Order Value"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r["Average Order Value"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 3, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Filtered queries
# ---------------------------------------------------------------------------


class TestMySQLFiltered:
    def test_where_in_filter(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
            where=[
                QueryFilter(
                    field="Customer Segment",
                    op=FilterOperator.IN,
                    value=["SMB", "MidMarket"],
                ),
            ],
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"
        assert rows[0]["Revenue"] == pytest.approx(200.0)

    def test_order_by_desc_with_limit(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=1,
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"


# ---------------------------------------------------------------------------
# Total measures (window functions)
# ---------------------------------------------------------------------------


class TestMySQLTotal:
    def test_grand_total_revenue(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        assert len(rows) == 2
        for row in rows:
            assert row["Grand Total Revenue"] == pytest.approx(240.0)

    def test_regular_and_total_together(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Grand Total Revenue"] == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# Metrics (derived measures)
# ---------------------------------------------------------------------------


class TestMySQLMetrics:
    def test_revenue_per_order(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue per Order"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue per Order"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 3, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(20.0)

    def test_revenue_share(self, mysql_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue Share"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "mysql").sql
        rows = _execute_dict(mysql_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue Share"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 240.0, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(40.0 / 240.0, rel=1e-3)
