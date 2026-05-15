"""Integration tests: compile queries and execute against a real PostgreSQL via testcontainers.

These tests validate that the Postgres dialect produces correct, executable SQL
against a real PostgreSQL database.  They are **opt-in** and require Docker:

    uv run pytest -m docker

Skipped automatically when:
- testcontainers or psycopg2 packages are not installed
- Docker is not running
"""

from __future__ import annotations

from typing import Any

import pytest

# Skip entire module if dependencies are missing
testcontainers_postgres = pytest.importorskip(
    "testcontainers.postgres", reason="testcontainers[postgres] required"
)
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2-binary required")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import (  # noqa: E402
    FilterOperator,
    Grouping,
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
# Test data (same values as test_duckdb_execution.py for baseline comparison)
# ---------------------------------------------------------------------------

# Postgres case-sensitivity: the compiled SQL uses double-quoted identifiers
# ("PUBLIC"."ORDERS", "CUSTOMER_ID") which are case-sensitive.  DDL must
# create a quoted uppercase schema and use quoted uppercase table/column names.
_SETUP_SQL = """\
CREATE SCHEMA "PUBLIC";

CREATE TABLE "PUBLIC"."CUSTOMERS" (
    "CUSTOMER_ID" VARCHAR, "NAME" VARCHAR, "COUNTRY" VARCHAR, "SEGMENT" VARCHAR
);
INSERT INTO "PUBLIC"."CUSTOMERS" VALUES
    ('C1', 'Alice',   'US', 'SMB'),
    ('C2', 'Bob',     'UK', 'Enterprise'),
    ('C3', 'Charlie', 'US', 'MidMarket');

CREATE TABLE "PUBLIC"."PRODUCTS" (
    "PRODUCT_ID" VARCHAR, "NAME" VARCHAR, "CATEGORY" VARCHAR
);
INSERT INTO "PUBLIC"."PRODUCTS" VALUES
    ('P1', 'Widget', 'Hardware'),
    ('P2', 'Gadget', 'Software');

CREATE TABLE "PUBLIC"."ORDERS" (
    "ORDER_ID" VARCHAR, "ORDER_DATE" DATE, "CUSTOMER_ID" VARCHAR,
    "PRODUCT_ID" VARCHAR, "QUANTITY" INTEGER, "PRICE" DOUBLE PRECISION
);
INSERT INTO "PUBLIC"."ORDERS" VALUES
    ('O1', '2024-01-15', 'C1', 'P1', 10,  5.0),
    ('O2', '2024-01-20', 'C1', 'P2',  2, 25.0),
    ('O3', '2024-02-10', 'C2', 'P1',  5,  5.0),
    ('O4', '2024-02-15', 'C3', 'P2',  1, 100.0),
    ('O5', '2024-03-01', 'C2', 'P1',  3,  5.0);
"""

# Expected values (identical to DuckDB baseline):
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
def postgres_conn():
    """Spin up a PostgreSQL container and return a psycopg2 connection.

    Skips if Docker is not running.
    """
    if not _docker_available():
        pytest.skip("Docker is not running")

    with PostgresContainer("postgres:16-alpine") as pg:
        conn = psycopg2.connect(
            host=pg.get_container_host_ip(),
            port=pg.get_exposed_port(5432),
            dbname=pg.dbname,
            user=pg.username,
            password=pg.password,
        )
        conn.autocommit = True
        cur = conn.cursor()
        # Execute setup DDL/DML statements one by one
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
    """Execute SQL on the Postgres connection and return rows as dicts."""
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Star-schema queries
# ---------------------------------------------------------------------------


class TestPostgresStarSchema:
    """Compile with dialect=postgres and execute against real PostgreSQL."""

    def test_revenue_by_country(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0)
        assert by_country["UK"] == pytest.approx(40.0)

    def test_order_count_by_country(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r["Order Count"] for r in rows}
        assert by_country["US"] == 3
        assert by_country["UK"] == 2

    def test_multi_measure(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Order Count"] == 3
        assert by_country["UK"]["Revenue"] == pytest.approx(40.0)
        assert by_country["UK"]["Order Count"] == 2

    def test_revenue_by_product_category(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Product Category"], measures=["Revenue"]),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_cat = {r["Product Category"]: r["Revenue"] for r in rows}
        assert by_cat["Hardware"] == pytest.approx(90.0)
        assert by_cat["Software"] == pytest.approx(150.0)

    def test_average_order_value(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Average Order Value"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r["Average Order Value"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 3, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Filtered queries
# ---------------------------------------------------------------------------


class TestPostgresFiltered:
    def test_where_in_filter(self, postgres_conn, sales_model, pipeline) -> None:
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
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"
        assert rows[0]["Revenue"] == pytest.approx(200.0)

    def test_order_by_desc_with_limit(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=1,
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"


# ---------------------------------------------------------------------------
# Total measures (window functions)
# ---------------------------------------------------------------------------


class TestPostgresTotal:
    def test_grand_total_revenue(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        assert len(rows) == 2
        for row in rows:
            assert row["Grand Total Revenue"] == pytest.approx(240.0)

    def test_regular_and_total_together(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Grand Total Revenue"] == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# Metrics (derived measures)
# ---------------------------------------------------------------------------


class TestPostgresMetrics:
    def test_revenue_per_order(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue per Order"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue per Order"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 3, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(20.0)

    def test_revenue_share(self, postgres_conn, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue Share"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue Share"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 240.0, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(40.0 / 240.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Grouping operators (ROLLUP / CUBE) — real execution
# ---------------------------------------------------------------------------


class TestPostgresRollupCube:
    """End-to-end: compile WITH ROLLUP/CUBE and execute against real Postgres.

    Verifies that the dialect emits executable SQL, that the auto-order
    NULLS FIRST default brings subtotals + grand total to the top of the
    result, and that GROUPING() flag columns correctly identify aggregate
    rows.
    """

    def test_rollup_single_dim_row_count_and_grand_total(
        self, postgres_conn, sales_model, pipeline
    ) -> None:
        """ROLLUP over 1 dim → N detail rows + 1 grand total."""
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
            grouping=Grouping.ROLLUP,
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        # 2 countries (US, UK) + 1 grand total = 3 rows.
        assert len(rows) == 3
        # NULLS FIRST default puts grand total at the top.
        assert rows[0]["Customer Country"] is None
        assert float(rows[0]["Revenue"]) == pytest.approx(240.0)
        # GROUPING flag column identifies the rolled-up total row.
        assert int(rows[0]["_g_Customer Country"]) == 1
        details = {r["Customer Country"]: r for r in rows[1:]}
        assert float(details["US"]["Revenue"]) == pytest.approx(200.0)
        assert int(details["US"]["_g_Customer Country"]) == 0
        assert float(details["UK"]["Revenue"]) == pytest.approx(40.0)

    def test_rollup_two_dims_country_subtotals(self, postgres_conn, sales_model, pipeline) -> None:
        """ROLLUP over 2 dims → details + per-first-dim subtotals + grand."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country", "Product Category"],
                measures=["Revenue"],
            ),
            grouping=Grouping.ROLLUP,
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        # Detail combinations: US×Hardware, US×Software, UK×Hardware = 3
        # Country subtotals (Product Category rolled up): US, UK = 2
        # Grand total: 1
        # Total: 6 rows
        assert len(rows) == 6

        # Grand total: both dims NULL, both GROUPING flags 1.
        grand = next(
            r for r in rows if r["Customer Country"] is None and r["Product Category"] is None
        )
        assert float(grand["Revenue"]) == pytest.approx(240.0)
        assert int(grand["_g_Customer Country"]) == 1
        assert int(grand["_g_Product Category"]) == 1

        # US subtotal: Country present, Product Category NULL.
        us_subtotal = next(
            r for r in rows if r["Customer Country"] == "US" and r["Product Category"] is None
        )
        assert float(us_subtotal["Revenue"]) == pytest.approx(200.0)
        assert int(us_subtotal["_g_Customer Country"]) == 0
        assert int(us_subtotal["_g_Product Category"]) == 1

    def test_cube_two_dims_full_lattice(self, postgres_conn, sales_model, pipeline) -> None:
        """CUBE over 2 dims → all subtotals on every axis + grand total."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country", "Product Category"],
                measures=["Revenue"],
            ),
            grouping=Grouping.CUBE,
        )
        sql = pipeline.compile(query, sales_model, "postgres").sql
        rows = _execute_dict(postgres_conn, sql)

        # CUBE: 3 details + 2 country subtotals + 2 category subtotals + 1 grand = 8
        assert len(rows) == 8

        # Hardware category subtotal across all countries.
        hw_subtotal = next(
            r for r in rows if r["Customer Country"] is None and r["Product Category"] == "Hardware"
        )
        assert float(hw_subtotal["Revenue"]) == pytest.approx(90.0)
        assert int(hw_subtotal["_g_Customer Country"]) == 1
        assert int(hw_subtotal["_g_Product Category"]) == 0

        # Software category subtotal.
        sw_subtotal = next(
            r for r in rows if r["Customer Country"] is None and r["Product Category"] == "Software"
        )
        assert float(sw_subtotal["Revenue"]) == pytest.approx(150.0)
