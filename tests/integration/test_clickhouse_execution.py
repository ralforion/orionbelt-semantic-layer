"""Integration tests: compile queries and execute against a real ClickHouse via testcontainers.

These tests validate that the ClickHouse dialect produces correct, executable SQL
against a real ClickHouse database.  They are **opt-in** and require Docker:

    uv run pytest -m docker

Skipped automatically when:
- testcontainers or clickhouse-connect packages are not installed
- Docker is not running
"""

from __future__ import annotations

from typing import Any

import pytest

# Skip entire module if dependencies are missing
testcontainers_clickhouse = pytest.importorskip(
    "testcontainers.clickhouse", reason="testcontainers[clickhouse] required"
)
clickhouse_connect = pytest.importorskip("clickhouse_connect", reason="clickhouse-connect required")

from testcontainers.clickhouse import ClickHouseContainer  # noqa: E402

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
# Test data (same values as other execution tests for baseline comparison)
# ---------------------------------------------------------------------------

# ClickHouse uses double-quote identifier quoting for columns and unquoted
# schema.table references (e.g. PUBLIC.ORDERS).  All tables require an
# ENGINE specification.  ClickHouse is fully case-sensitive.
_SETUP_SQL = """\
CREATE DATABASE IF NOT EXISTS PUBLIC;

CREATE TABLE PUBLIC.CUSTOMERS (
    CUSTOMER_ID String, NAME String, COUNTRY String, SEGMENT String
) ENGINE = MergeTree() ORDER BY tuple();
INSERT INTO PUBLIC.CUSTOMERS VALUES
    ('C1', 'Alice',   'US', 'SMB'),
    ('C2', 'Bob',     'UK', 'Enterprise'),
    ('C3', 'Charlie', 'US', 'MidMarket');

CREATE TABLE PUBLIC.PRODUCTS (
    PRODUCT_ID String, NAME String, CATEGORY String
) ENGINE = MergeTree() ORDER BY tuple();
INSERT INTO PUBLIC.PRODUCTS VALUES
    ('P1', 'Widget', 'Hardware'),
    ('P2', 'Gadget', 'Software');

CREATE TABLE PUBLIC.ORDERS (
    ORDER_ID String, ORDER_DATE Date, CUSTOMER_ID String,
    PRODUCT_ID String, QUANTITY Int32, PRICE Float64
) ENGINE = MergeTree() ORDER BY tuple();
INSERT INTO PUBLIC.ORDERS VALUES
    ('O1', '2024-01-15', 'C1', 'P1', 10,  5.0),
    ('O2', '2024-01-20', 'C1', 'P2',  2, 25.0),
    ('O3', '2024-02-10', 'C2', 'P1',  5,  5.0),
    ('O4', '2024-02-15', 'C3', 'P2',  1, 100.0),
    ('O5', '2024-03-01', 'C2', 'P1',  3,  5.0);
"""

# Expected values (identical to DuckDB/Postgres/MySQL baseline):
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
def ch_client():
    """Spin up a ClickHouse container and return a clickhouse-connect client.

    Skips if Docker is not running.
    """
    if not _docker_available():
        pytest.skip("Docker is not running")

    with ClickHouseContainer("clickhouse/clickhouse-server:latest") as ch:
        client = clickhouse_connect.get_client(
            host=ch.get_container_host_ip(),
            port=int(ch.get_exposed_port(8123)),
            username=ch.username,
            password=ch.password,
        )
        for stmt in _SETUP_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                client.command(stmt)
        yield client
        client.close()


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


def _execute_dict(client: Any, sql: str) -> list[dict[str, Any]]:
    """Execute SQL on the ClickHouse client and return rows as dicts."""
    result = client.query(sql)
    return [dict(zip(result.column_names, row, strict=False)) for row in result.result_rows]


# ---------------------------------------------------------------------------
# Star-schema queries
# ---------------------------------------------------------------------------


class TestClickHouseStarSchema:
    """Compile with dialect=clickhouse and execute against real ClickHouse."""

    def test_revenue_by_country(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r["Revenue"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0)
        assert by_country["UK"] == pytest.approx(40.0)

    def test_order_count_by_country(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r["Order Count"] for r in rows}
        assert by_country["US"] == 3
        assert by_country["UK"] == 2

    def test_multi_measure(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Order Count"] == 3
        assert by_country["UK"]["Revenue"] == pytest.approx(40.0)
        assert by_country["UK"]["Order Count"] == 2

    def test_revenue_by_product_category(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Product Category"], measures=["Revenue"]),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_cat = {r["Product Category"]: r["Revenue"] for r in rows}
        assert by_cat["Hardware"] == pytest.approx(90.0)
        assert by_cat["Software"] == pytest.approx(150.0)

    def test_average_order_value(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Average Order Value"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r["Average Order Value"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 3, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Filtered queries
# ---------------------------------------------------------------------------


class TestClickHouseFiltered:
    def test_where_in_filter(self, ch_client, sales_model, pipeline) -> None:
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
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"
        assert rows[0]["Revenue"] == pytest.approx(200.0)

    def test_order_by_desc_with_limit(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=1,
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"


# ---------------------------------------------------------------------------
# Total measures (window functions)
# ---------------------------------------------------------------------------


class TestClickHouseTotal:
    def test_grand_total_revenue(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        assert len(rows) == 2
        for row in rows:
            assert row["Grand Total Revenue"] == pytest.approx(240.0)

    def test_regular_and_total_together(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Grand Total Revenue"] == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# Metrics (derived measures)
# ---------------------------------------------------------------------------


class TestClickHouseMetrics:
    def test_revenue_per_order(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue per Order"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r["Revenue per Order"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 3, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(20.0)

    def test_revenue_share(self, ch_client, sales_model, pipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue Share"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        by_country = {r["Customer Country"]: r["Revenue Share"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 240.0, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(40.0 / 240.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Grouping operators (ROLLUP / CUBE) — real execution
# ---------------------------------------------------------------------------


class TestClickHouseRollupCube:
    """End-to-end: compile WITH ROLLUP/CUBE and execute against real ClickHouse.

    Verifies that the dialect emits executable SQL, that the auto-order
    NULLS FIRST default brings subtotals + grand total to the top of the
    result, and that GROUPING() flag columns correctly identify aggregate
    rows.
    """

    def test_rollup_single_dim_row_count_and_grand_total(
        self, ch_client, sales_model, pipeline
    ) -> None:
        """ROLLUP over 1 dim → N detail rows + 1 grand total."""
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
            grouping=Grouping.ROLLUP,
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        # 2 countries (US, UK) + 1 grand total = 3 rows.
        assert len(rows) == 3
        # GROUPING flag identifies the rolled-up total row authoritatively.
        # NULLS FIRST default puts that row at the top. ClickHouse returns
        # an empty string for rolled-up non-Nullable String dims (rather
        # than NULL), so we key off the GROUPING flag, not the dim value.
        assert int(rows[0]["_g_Customer Country"]) == 1
        assert float(rows[0]["Revenue"]) == pytest.approx(240.0)
        details = {r["Customer Country"]: r for r in rows[1:]}
        assert float(details["US"]["Revenue"]) == pytest.approx(200.0)
        assert int(details["US"]["_g_Customer Country"]) == 0
        assert float(details["UK"]["Revenue"]) == pytest.approx(40.0)

    def test_rollup_two_dims_country_subtotals(self, ch_client, sales_model, pipeline) -> None:
        """ROLLUP over 2 dims → details + per-first-dim subtotals + grand."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country", "Product Category"],
                measures=["Revenue"],
            ),
            grouping=Grouping.ROLLUP,
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        # Detail combinations: US×Hardware, US×Software, UK×Hardware = 3
        # Country subtotals (Product Category rolled up): US, UK = 2
        # Grand total: 1
        # Total: 6 rows
        assert len(rows) == 6

        # Grand total: both GROUPING flags = 1.
        grand = next(
            r
            for r in rows
            if int(r["_g_Customer Country"]) == 1 and int(r["_g_Product Category"]) == 1
        )
        assert float(grand["Revenue"]) == pytest.approx(240.0)

        # US subtotal: Country flag=0, Product Category flag=1 (rolled up).
        us_subtotal = next(
            r for r in rows if r["Customer Country"] == "US" and int(r["_g_Product Category"]) == 1
        )
        assert float(us_subtotal["Revenue"]) == pytest.approx(200.0)
        assert int(us_subtotal["_g_Customer Country"]) == 0

    def test_cube_two_dims_full_lattice(self, ch_client, sales_model, pipeline) -> None:
        """CUBE over 2 dims → all subtotals on every axis + grand total."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country", "Product Category"],
                measures=["Revenue"],
            ),
            grouping=Grouping.CUBE,
        )
        sql = pipeline.compile(query, sales_model, "clickhouse").sql
        rows = _execute_dict(ch_client, sql)

        # CUBE: 3 details + 2 country subtotals + 2 category subtotals + 1 grand = 8
        assert len(rows) == 8

        # Hardware category subtotal: Country rolled up (flag=1), Hardware kept.
        hw_subtotal = next(
            r
            for r in rows
            if int(r["_g_Customer Country"]) == 1 and r["Product Category"] == "Hardware"
        )
        assert float(hw_subtotal["Revenue"]) == pytest.approx(90.0)

        # Software category subtotal: Country rolled up, Software kept.
        sw_subtotal = next(
            r
            for r in rows
            if int(r["_g_Customer Country"]) == 1 and r["Product Category"] == "Software"
        )
        assert float(sw_subtotal["Revenue"]) == pytest.approx(150.0)
