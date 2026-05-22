"""End-to-end compilation tests using the sales model fixture."""

from __future__ import annotations

import re

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import QueryResolver
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SALES_MODEL_DIR


def _load_sales_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Errors: {[e.message for e in result.errors]}"
    return model


class TestEndToEndCompilation:
    @pytest.fixture
    def model(self) -> SemanticModel:
        return _load_sales_model()

    @pytest.fixture
    def pipeline(self) -> CompilationPipeline:
        return CompilationPipeline()

    def test_revenue_by_country_postgres(
        self, model: SemanticModel, pipeline: CompilationPipeline
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count"],
            ),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=1000,
        )
        result = pipeline.compile(query, model, "postgres")
        assert "SELECT" in result.sql
        assert "GROUP BY" in result.sql
        assert "LIMIT 1000" in result.sql
        assert result.dialect == "postgres"
        assert "Customer Country" in result.resolved.dimensions
        assert "Revenue" in result.resolved.measures
        assert "Order Count" in result.resolved.measures

    def test_revenue_by_country_snowflake(
        self, model: SemanticModel, pipeline: CompilationPipeline
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "snowflake")
        assert "SELECT" in result.sql
        assert result.dialect == "snowflake"

    def test_product_dimension(self, model: SemanticModel, pipeline: CompilationPipeline) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Product Name"],
                measures=["Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert "SELECT" in result.sql
        assert "Product Name" in result.resolved.dimensions

    def test_data_object_dimension(
        self, model: SemanticModel, pipeline: CompilationPipeline
    ) -> None:
        """Test dimension defined with dataObject + field syntax."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Product Category"],
                measures=["Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert "SELECT" in result.sql
        assert "Product Category" in result.resolved.dimensions

    def test_with_where_filter(self, model: SemanticModel, pipeline: CompilationPipeline) -> None:
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
        result = pipeline.compile(query, model, "postgres")
        assert "WHERE" in result.sql
        assert "SELECT" in result.sql

    @pytest.mark.parametrize(
        "dialect",
        ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"],
    )
    def test_all_dialects(
        self, model: SemanticModel, pipeline: CompilationPipeline, dialect: str
    ) -> None:
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        assert "SELECT" in result.sql
        assert result.dialect == dialect

    @pytest.mark.parametrize(
        "dialect",
        ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"],
    )
    def test_metric_revenue_per_order_all_dialects(
        self, model: SemanticModel, pipeline: CompilationPipeline, dialect: str
    ) -> None:
        """Metric 'Revenue per Order' compiles to valid SQL across all dialects."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue per Order"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        sql = result.sql
        assert "SELECT" in sql
        assert "GROUP BY" in sql
        assert "_ref_" not in sql
        assert "Revenue per Order" in sql
        assert result.dialect == dialect

    def test_metric_with_regular_measures(
        self, model: SemanticModel, pipeline: CompilationPipeline
    ) -> None:
        """Metric combined with regular measures produces valid SQL."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Order Count", "Revenue per Order"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql
        assert "Revenue per Order" in sql
        assert "_ref_" not in sql

    @pytest.mark.parametrize(
        "dialect",
        ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"],
    )
    def test_total_measure_all_dialects(
        self, model: SemanticModel, pipeline: CompilationPipeline, dialect: str
    ) -> None:
        """Total measure produces wrapper CTE with window function across all dialects."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Grand Total Revenue"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        sql = result.sql
        assert "OVER ()" in sql
        assert "Grand Total Revenue" in sql
        assert result.dialect == dialect

    @pytest.mark.parametrize(
        "dialect",
        ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"],
    )
    def test_total_with_regular_measure_all_dialects(
        self, model: SemanticModel, pipeline: CompilationPipeline, dialect: str
    ) -> None:
        """Total + regular measure in same query across all dialects."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Grand Total Revenue"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        sql = result.sql
        assert "OVER ()" in sql
        assert "Grand Total Revenue" in sql
        assert "Revenue" in sql

    @pytest.mark.parametrize(
        "dialect",
        ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"],
    )
    def test_revenue_share_metric_all_dialects(
        self, model: SemanticModel, pipeline: CompilationPipeline, dialect: str
    ) -> None:
        """Revenue Share metric (with total component) across all dialects."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue Share"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        sql = result.sql
        assert "OVER ()" in sql
        assert "Revenue Share" in sql
        assert result.dialect == dialect

    def test_total_measure_with_limit_and_order(
        self, model: SemanticModel, pipeline: CompilationPipeline
    ) -> None:
        """Total measure with ORDER BY and LIMIT — limit on outer, not base CTE."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Grand Total Revenue"],
            ),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
            limit=10,
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql
        assert "OVER ()" in sql
        assert "LIMIT 10" in sql
        assert "ORDER BY" in sql


# ---------------------------------------------------------------------------
# CFL (Composite Fact Layer) with filter tests
# ---------------------------------------------------------------------------

CFL_MODEL_YAML = """\
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

  Products:
    code: PRODUCTS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Product ID:
        code: PRODUCT_ID
        abstractType: string
      Category:
        code: CATEGORY
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
      Order Product ID:
        code: PRODUCT_ID
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
      - joinType: many-to-one
        joinTo: Products
        columnsFrom:
          - Order Product ID
        columnsTo:
          - Product ID

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
  Product Category:
    dataObject: Products
    column: Category
    resultType: string

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
  Electronics Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    filters:
      - column: {dataObject: Products, column: Category}
        operator: equals
        values: [{dataType: string, valueString: "Electronics"}]
    resultType: float
    aggregation: sum
  Total Refunds:
    columns:
      - dataObject: Returns
        column: Refund
    resultType: float
    aggregation: sum

metrics:
  Refund Ratio:
    expression: "{[Total Refunds]} / {[Revenue]}"
    dataType: "decimal(5, 4)"
"""


def _load_cfl_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(CFL_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Errors: {[e.message for e in result.errors]}"
    return model


class TestCFLWithFilters:
    """Tests for CFL planner with WHERE and HAVING filters."""

    def test_cfl_triggers_for_multi_fact(self) -> None:
        """Revenue + Total Refunds span Orders and Returns — triggers CFL."""
        model = _load_cfl_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
        )
        resolved = resolver.resolve(query, model)
        assert resolved.requires_cfl

    def test_cfl_with_where_filter(self) -> None:
        """CFL query with WHERE filter — filter appears in generated SQL."""
        model = _load_cfl_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
            where=[
                QueryFilter(field="Customer Country", op=FilterOperator.EQ, value="US"),
            ],
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql
        assert "composite_01" in sql  # CFL CTE
        assert "WHERE" in sql
        assert "'US'" in sql

    def test_cfl_with_order_by_measure(self) -> None:
        """CFL ORDER BY on a measure uses CTE alias, not original table ref."""
        model = _load_cfl_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
            order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql
        assert "composite_01" in sql
        assert "ORDER BY" in sql
        # Should NOT reference original table in ORDER BY
        order_part = sql.split("ORDER BY")[1]
        assert '"Orders"' not in order_part
        assert '"Revenue"' in order_part

    def test_cfl_leg_joins_objects_referenced_by_measure_filter(self) -> None:
        """A filtered measure's FROM must include every table its filter touches.

        ``Electronics Revenue`` is defined on Orders.Amount but its filter
        references ``Products.Category``. In a multi-fact CFL query the
        Orders-side leg's projection becomes
        ``CASE WHEN Products.Category = 'Electronics' THEN Orders.Amount END``,
        so the leg's FROM must JOIN Products. Without this fix the leg
        emits ``"Products"."Category"`` against a FROM that only has
        ``Orders LEFT JOIN Customers``, and the database returns
        ``missing FROM-clause entry for table "Products"``.
        """
        model = _load_cfl_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Electronics Revenue", "Total Refunds"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql
        assert "composite_01" in sql  # CFL CTE
        assert "UNION ALL" in sql
        # The Orders-side leg must JOIN Products so the filter resolves.
        orders_leg = sql.split("UNION ALL")[0]
        assert "Products" in orders_leg, (
            f"Orders-side CFL leg missing Products JOIN — would error "
            f'`missing FROM-clause entry for table "Products"`. SQL:\n{orders_leg}'
        )

    def test_cfl_without_filter(self) -> None:
        """CFL query without filters — no WHERE clause."""
        model = _load_cfl_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql
        assert "composite_01" in sql
        assert "UNION ALL" in sql

    @pytest.mark.parametrize(
        "dialect",
        ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"],
    )
    def test_cfl_metric_qualifies_inner_refs_with_cte_alias(self, dialect: str) -> None:
        """CFL outer-aggregate ColumnRefs are qualified with the CTE alias.

        Regression: ClickHouse rejects ``SUM("Total Refunds") / SUM("Revenue")``
        in the outer SELECT as ILLEGAL_AGGREGATION when those bare identifiers
        shadow sibling SELECT aliases that are themselves aggregates. The
        planner now qualifies every ColumnRef inside outer aggregates with
        the composite CTE name (``composite_01``) so the inner refs resolve
        to raw CTE columns instead of sibling aggregate aliases.
        """
        model = _load_cfl_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue", "Total Refunds", "Refund Ratio"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        sql = result.sql
        assert "composite_01" in sql
        # Every reference inside an outer SUM(...) must be qualified with the
        # CTE alias. Identifier quoting differs per dialect, so match on the
        # quote-agnostic shape: SUM( <quote>composite_01<quote>.<quote>...
        assert re.search(r"SUM\(\W?composite_01\W?\.\W?Revenue\W?\)", sql), sql
        assert re.search(r"SUM\(\W?composite_01\W?\.\W?Total Refunds\W?\)", sql), sql
