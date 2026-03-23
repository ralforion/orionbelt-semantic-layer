"""Integration tests: compile queries and execute against in-memory DuckDB.

Validates that the DuckDB dialect produces correct, executable SQL that
returns expected results for deterministic test data.  Covers star-schema,
multi-measure, filtered, total (window), metric, and CFL (multi-fact) queries.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb package required for execution tests")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.api.deps import init_session_manager, reset_session_manager  # noqa: E402
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
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402
from tests.conftest import SALES_MODEL_DIR, SAMPLE_MODEL_YAML  # noqa: E402

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_SETUP_SALES_SQL = """\
CREATE SCHEMA IF NOT EXISTS PUBLIC;

CREATE TABLE PUBLIC.CUSTOMERS (
    CUSTOMER_ID VARCHAR, NAME VARCHAR, COUNTRY VARCHAR, SEGMENT VARCHAR
);
INSERT INTO PUBLIC.CUSTOMERS VALUES
    ('C1', 'Alice',   'US', 'SMB'),
    ('C2', 'Bob',     'UK', 'Enterprise'),
    ('C3', 'Charlie', 'US', 'MidMarket');

CREATE TABLE PUBLIC.PRODUCTS (
    PRODUCT_ID VARCHAR, NAME VARCHAR, CATEGORY VARCHAR
);
INSERT INTO PUBLIC.PRODUCTS VALUES
    ('P1', 'Widget', 'Hardware'),
    ('P2', 'Gadget', 'Software');

CREATE TABLE PUBLIC.ORDERS (
    ORDER_ID VARCHAR, ORDER_DATE DATE, CUSTOMER_ID VARCHAR,
    PRODUCT_ID VARCHAR, QUANTITY INTEGER, PRICE DOUBLE
);
INSERT INTO PUBLIC.ORDERS VALUES
    ('O1', '2024-01-15', 'C1', 'P1', 10,  5.0),
    ('O2', '2024-01-20', 'C1', 'P2',  2, 25.0),
    ('O3', '2024-02-10', 'C2', 'P1',  5,  5.0),
    ('O4', '2024-02-15', 'C3', 'P2',  1, 100.0),
    ('O5', '2024-03-01', 'C2', 'P1',  3,  5.0);
"""

# Pre-calculated expected values:
# Revenue per order: O1=50, O2=50, O3=25, O4=100, O5=15
# Revenue by country:  US = 200.0  (C1: 50+50, C3: 100)
#                       UK = 40.0   (C2: 25+15)
# Order count:         US = 3, UK = 2
# Grand Total Revenue: 240.0
# Revenue per Order:   US ≈ 66.667, UK = 20.0
# Revenue Share:       US ≈ 0.833,  UK ≈ 0.167

# CFL model: Orders + Returns as independent fact tables sharing Customers
_CFL_MODEL_YAML = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Order Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Order Customer ID
        columnsTo:
          - Customer ID

  Returns:
    code: RETURNS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Return ID:
        code: RETURN_ID
        abstractType: string
      Return Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Refund:
        code: REFUND
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Return Customer ID
        columnsTo:
          - Customer ID

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
  Total Refunds:
    columns:
      - dataObject: Returns
        column: Refund
    resultType: float
    aggregation: sum
"""

_SETUP_CFL_SQL = """\
CREATE TABLE PUBLIC.RETURNS (
    RETURN_ID VARCHAR, CUSTOMER_ID VARCHAR, REFUND DOUBLE
);
INSERT INTO PUBLIC.RETURNS VALUES
    ('R1', 'C1', 20.0),
    ('R2', 'C2', 10.0);

-- CFL also needs ORDERS with an AMOUNT column
CREATE OR REPLACE TABLE PUBLIC.ORDERS AS (
    SELECT ORDER_ID, CUSTOMER_ID, PRODUCT_ID, QUANTITY, PRICE,
           (PRICE * QUANTITY) AS AMOUNT, ORDER_DATE
    FROM PUBLIC.ORDERS
);
"""

# CFL expected: Revenue by country: US=200, UK=40; Refunds: US=20, UK=10


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def duckdb_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with test tables matching the sales model."""
    conn = duckdb.connect(":memory:")
    conn.execute(_SETUP_SALES_SQL)
    conn.execute(_SETUP_CFL_SQL)
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
def cfl_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(_CFL_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


@pytest.fixture(scope="module")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


def _execute(conn: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    """Execute SQL on the test DuckDB and return rows as tuples."""
    return conn.execute(sql).fetchall()


def _execute_dict(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    """Execute SQL and return rows as dicts keyed by column name."""
    result = conn.execute(sql)
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, row, strict=False)) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# Star-schema queries
# ---------------------------------------------------------------------------


class TestStarSchemaExecution:
    """Compile and execute single-fact star-schema queries."""

    def test_revenue_by_country(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0)
        assert by_country["UK"] == pytest.approx(40.0)

    def test_order_count_by_country(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Order Count"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r["Order Count"] for r in rows}
        assert by_country["US"] == 3
        assert by_country["UK"] == 2

    def test_multi_measure(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """Multiple measures in a single query."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Order Count"] == 3
        assert by_country["UK"]["Revenue"] == pytest.approx(40.0)
        assert by_country["UK"]["Order Count"] == 2

    def test_revenue_by_product_category(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """Join through Products dimension table."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Product Category"],
                measures=["Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_cat = {r["Product Category"]: r["Revenue"] for r in rows}
        assert by_cat["Hardware"] == pytest.approx(90.0)  # P1: 50+25+15
        assert by_cat["Software"] == pytest.approx(150.0)  # P2: 50+100

    def test_average_order_value(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """AVG aggregation."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Average Order Value"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r["Average Order Value"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0 / 3, rel=1e-3)
        assert by_country["UK"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Filtered queries
# ---------------------------------------------------------------------------


class TestFilteredExecution:
    """Queries with WHERE filters, ORDER BY, and LIMIT."""

    def test_where_in_filter(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """IN filter narrows the result set."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
            where=[
                QueryFilter(
                    field="Customer Segment",
                    op=FilterOperator.IN,
                    value=["SMB", "MidMarket"],
                ),
            ],
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        # Only US customers: C1 (SMB) and C3 (MidMarket)
        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"
        assert rows[0]["Revenue"] == pytest.approx(200.0)

    def test_where_eq_filter(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
            where=[
                QueryFilter(field="Customer Country", op=FilterOperator.EQ, value="UK"),
            ],
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        assert len(rows) == 1
        assert rows[0]["Revenue"] == pytest.approx(40.0)

    def test_order_by_desc_with_limit(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=1,
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"
        assert rows[0]["Revenue"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Total measures (window functions)
# ---------------------------------------------------------------------------


class TestTotalMeasureExecution:
    """Queries with total=true measures (OVER () window aggregation)."""

    def test_grand_total_revenue(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        # Grand total should be 240 for every row
        assert len(rows) == 2
        for row in rows:
            assert row["Grand Total Revenue"] == pytest.approx(240.0)

    def test_regular_and_total_together(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """Regular measure + total measure in the same query."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Grand Total Revenue"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Grand Total Revenue"] == pytest.approx(240.0)
        assert by_country["UK"]["Revenue"] == pytest.approx(40.0)
        assert by_country["UK"]["Grand Total Revenue"] == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# Metrics (derived measures)
# ---------------------------------------------------------------------------


class TestMetricExecution:
    """Queries with metric expressions (derived from measures)."""

    def test_revenue_per_order(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue per Order"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue per Order"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0 / 3, rel=1e-3)
        assert by_country["UK"] == pytest.approx(20.0)

    def test_revenue_share(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """Metric using a total measure — Revenue / Grand Total Revenue."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue Share"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r["Revenue Share"] for r in rows}
        assert by_country["US"] == pytest.approx(200.0 / 240.0, rel=1e-3)
        assert by_country["UK"] == pytest.approx(40.0 / 240.0, rel=1e-3)

    def test_metric_with_regular_measures(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        sales_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """Metric + regular measures in the same query."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count", "Revenue per Order"],
            ),
        )
        sql = pipeline.compile(query, sales_model, "duckdb").sql
        rows = _execute_dict(duckdb_conn, sql)

        by_country = {r["Customer Country"]: r for r in rows}
        us = by_country["US"]
        assert us["Revenue"] == pytest.approx(200.0)
        assert us["Order Count"] == 3
        assert us["Revenue per Order"] == pytest.approx(200.0 / 3, rel=1e-3)


# ---------------------------------------------------------------------------
# CFL (Composite Fact Layer) — multi-fact queries
# ---------------------------------------------------------------------------


class TestCFLExecution:
    """CFL queries spanning independent fact tables (UNION ALL strategy)."""

    def test_cfl_revenue_and_refunds(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        cfl_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
        )
        result = pipeline.compile(query, cfl_model, "duckdb")
        assert "composite_01" in result.sql  # CFL activated (UNION ALL strategy)
        rows = _execute_dict(duckdb_conn, result.sql)

        by_country = {r["Customer Country"]: r for r in rows}
        assert by_country["US"]["Revenue"] == pytest.approx(200.0)
        assert by_country["US"]["Total Refunds"] == pytest.approx(20.0)
        assert by_country["UK"]["Revenue"] == pytest.approx(40.0)
        assert by_country["UK"]["Total Refunds"] == pytest.approx(10.0)

    def test_cfl_with_where_filter(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        cfl_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
            where=[
                QueryFilter(field="Customer Country", op=FilterOperator.EQ, value="US"),
            ],
        )
        result = pipeline.compile(query, cfl_model, "duckdb")
        rows = _execute_dict(duckdb_conn, result.sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"
        assert rows[0]["Revenue"] == pytest.approx(200.0)
        assert rows[0]["Total Refunds"] == pytest.approx(20.0)

    def test_cfl_with_order_by_and_limit(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        cfl_model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=1,
        )
        result = pipeline.compile(query, cfl_model, "duckdb")
        rows = _execute_dict(duckdb_conn, result.sql)

        assert len(rows) == 1
        assert rows[0]["Customer Country"] == "US"


# ---------------------------------------------------------------------------
# API endpoint integration (POST /query/execute)
# ---------------------------------------------------------------------------

# For the SAMPLE_MODEL_YAML, tables are: PUBLIC.CUSTOMERS, PUBLIC.ORDERS
_SETUP_SAMPLE_SQL = """\
CREATE SCHEMA IF NOT EXISTS PUBLIC;

CREATE OR REPLACE TABLE PUBLIC.CUSTOMERS (
    CUSTOMER_ID VARCHAR, COUNTRY VARCHAR
);
INSERT INTO PUBLIC.CUSTOMERS VALUES ('C1', 'US'), ('C2', 'UK');

CREATE OR REPLACE TABLE PUBLIC.ORDERS (
    ORDER_ID VARCHAR, CUSTOMER_ID VARCHAR, AMOUNT DOUBLE
);
INSERT INTO PUBLIC.ORDERS VALUES
    ('O1', 'C1', 100.0),
    ('O2', 'C1', 50.0),
    ('O3', 'C2', 75.0);
"""


def _make_execute_sql(conn: duckdb.DuckDBPyConnection):
    """Create a mock execute_sql that uses the test DuckDB connection."""

    import time

    def execute_sql(sql: str, *, dialect: str) -> ExecutionResult:
        t0 = time.monotonic()
        result = conn.execute(sql)
        raw_rows = result.fetchall()
        desc = result.description or []
        columns = [ColumnMeta(name=d[0], type_hint="string") for d in desc]
        rows = [list(r) for r in raw_rows]
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ExecutionResult(
            columns=columns,
            raw_rows=rows,
            row_count=len(rows),
            execution_time_ms=round(elapsed_ms, 2),
        )

    return execute_sql


class TestAPIExecuteEndpoint:
    """POST /query/execute against a real DuckDB via mocked execute_sql."""

    @pytest.fixture
    def api_duckdb(self) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(":memory:")
        conn.execute(_SETUP_SAMPLE_SQL)
        yield conn
        conn.close()

    async def test_execute_returns_rows(self, api_duckdb: duckdb.DuckDBPyConnection) -> None:
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(mgr, query_execute_enabled=True, db_vendor="duckdb")
        try:
            mock_exec = _make_execute_sql(api_duckdb)
            with patch("orionbelt.api.routers.sessions.execute_sql", mock_exec):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as c:
                    sid = (await c.post("/v1/sessions")).json()["session_id"]
                    load = await c.post(
                        f"/v1/sessions/{sid}/models",
                        json={"model_yaml": SAMPLE_MODEL_YAML},
                    )
                    mid = load.json()["model_id"]
                    response = await c.post(
                        f"/v1/sessions/{sid}/query/execute",
                        json={
                            "model_id": mid,
                            "query": {
                                "select": {
                                    "dimensions": ["Customer Country"],
                                    "measures": ["Total Revenue"],
                                },
                            },
                            "dialect": "duckdb",
                        },
                    )
            assert response.status_code == 200
            data = response.json()
            assert data["row_count"] == 2
            assert len(data["rows"]) == 2
            assert len(data["columns"]) == 2
            assert data["dialect"] == "duckdb"
            assert "sql" in data
            assert data["execution_time_ms"] > 0

            # Verify actual values
            col_names = [c["name"] for c in data["columns"]]
            rows_as_dicts = [dict(zip(col_names, row, strict=False)) for row in data["rows"]]
            by_country = {r["Customer Country"]: r for r in rows_as_dicts}
            assert by_country["US"]["Total Revenue"] == pytest.approx(150.0)
            assert by_country["UK"]["Total Revenue"] == pytest.approx(75.0)
        finally:
            reset_session_manager()

    async def test_execute_shortcut_endpoint(self, api_duckdb: duckdb.DuckDBPyConnection) -> None:
        """Top-level /v1/query/execute shortcut with single-model mode."""
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(
            mgr,
            query_execute_enabled=True,
            db_vendor="duckdb",
            preload_model_yaml=SAMPLE_MODEL_YAML,
        )
        try:
            mock_exec = _make_execute_sql(api_duckdb)
            with (
                patch("orionbelt.api.routers.sessions.execute_sql", mock_exec),
                patch("orionbelt.api.routers.shortcuts.execute_sql", mock_exec),
            ):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as c:
                    # Create a session so the shortcut can auto-resolve
                    await c.post("/v1/sessions")
                    response = await c.post(
                        "/v1/query/execute",
                        json={
                            "select": {
                                "dimensions": ["Customer Country"],
                                "measures": ["Order Count"],
                            },
                        },
                        params={"dialect": "duckdb"},
                    )
            assert response.status_code == 200
            data = response.json()
            assert data["row_count"] == 2

            col_names = [c["name"] for c in data["columns"]]
            rows_as_dicts = [dict(zip(col_names, row, strict=False)) for row in data["rows"]]
            by_country = {r["Customer Country"]: r for r in rows_as_dicts}
            assert by_country["US"]["Order Count"] == 2
            assert by_country["UK"]["Order Count"] == 1
        finally:
            reset_session_manager()

    async def test_execute_with_limit(self, api_duckdb: duckdb.DuckDBPyConnection) -> None:
        """Default limit is applied when query has no explicit limit."""
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(
            mgr, query_execute_enabled=True, db_vendor="duckdb", query_default_limit=1
        )
        try:
            mock_exec = _make_execute_sql(api_duckdb)
            with patch("orionbelt.api.routers.sessions.execute_sql", mock_exec):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as c:
                    sid = (await c.post("/v1/sessions")).json()["session_id"]
                    load = await c.post(
                        f"/v1/sessions/{sid}/models",
                        json={"model_yaml": SAMPLE_MODEL_YAML},
                    )
                    mid = load.json()["model_id"]
                    response = await c.post(
                        f"/v1/sessions/{sid}/query/execute",
                        json={
                            "model_id": mid,
                            "query": {
                                "select": {
                                    "dimensions": ["Customer Country"],
                                    "measures": ["Total Revenue"],
                                },
                            },
                            "dialect": "duckdb",
                        },
                    )
            assert response.status_code == 200
            data = response.json()
            # Default limit=1, so only one row
            assert data["row_count"] == 1
            assert "LIMIT 1" in data["sql"]
        finally:
            reset_session_manager()
