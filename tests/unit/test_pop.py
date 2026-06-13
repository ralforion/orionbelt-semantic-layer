"""Tests for period-over-period (PoP) metrics: model, resolution, wrapping, and SQL generation."""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import (
    QueryResolver,
    ResolutionError,
)
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import (
    Metric,
    MetricType,
    PeriodOverPeriod,
    PeriodOverPeriodComparison,
    SemanticModel,
    TimeGrain,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# ── OBML YAML with PoP metrics ──────────────────────────────────────────

POP_MODEL_YAML = """\
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
      Order Date:
        code: ORDER_DATE
        abstractType: date
      Order Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Order Customer ID
        columnsTo:
          - Customer ID

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month
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

  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count

metrics:
  Revenue per Order:
    expression: '{[Revenue]} / {[Order Count]}'

  Revenue YoY Growth:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: year
      comparison: percentChange

  Revenue MoM Diff:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference

  Revenue Prev Year:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: year
      comparison: previousValue

  Revenue YoY Ratio:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: year
      comparison: ratio
"""


def _load_model(yaml_content: str = POP_MODEL_YAML) -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(yaml_content)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Model errors: {[e.message for e in result.errors]}"
    return model


# ── Model parsing tests ────────────────────────────────────────────────────


class TestPoPModel:
    def test_pop_metric_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["Revenue YoY Growth"]
        assert m.type == MetricType.PERIOD_OVER_PERIOD
        assert m.expression == "{[Revenue]}"
        assert m.period_over_period is not None
        assert m.period_over_period.time_dimension == "Order Date"
        assert m.period_over_period.grain == TimeGrain.MONTH
        assert m.period_over_period.offset == -1
        assert m.period_over_period.offset_grain == TimeGrain.YEAR
        assert m.period_over_period.comparison == PeriodOverPeriodComparison.PERCENT_CHANGE

    def test_pop_difference_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["Revenue MoM Diff"]
        assert m.period_over_period is not None
        assert m.period_over_period.comparison == PeriodOverPeriodComparison.DIFFERENCE
        assert m.period_over_period.offset_grain == TimeGrain.MONTH

    def test_pop_previous_value_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["Revenue Prev Year"]
        assert m.period_over_period is not None
        assert m.period_over_period.comparison == PeriodOverPeriodComparison.PREVIOUS_VALUE

    def test_pop_ratio_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["Revenue YoY Ratio"]
        assert m.period_over_period is not None
        assert m.period_over_period.comparison == PeriodOverPeriodComparison.RATIO

    def test_derived_metric_unchanged(self) -> None:
        model = _load_model()
        m = model.metrics["Revenue per Order"]
        assert m.type == MetricType.DERIVED
        assert m.period_over_period is None


class TestPoPValidation:
    def test_pop_requires_expression(self) -> None:
        with pytest.raises(ValueError, match="expression"):
            Metric(
                label="Bad",
                type=MetricType.PERIOD_OVER_PERIOD,
                period_over_period=PeriodOverPeriod(
                    time_dimension="D",
                    grain=TimeGrain.MONTH,
                    offset_grain=TimeGrain.YEAR,
                ),
            )

    def test_pop_requires_period_over_period(self) -> None:
        with pytest.raises(ValueError, match="periodOverPeriod"):
            Metric(
                label="Bad",
                type=MetricType.PERIOD_OVER_PERIOD,
                expression="{[Revenue]}",
            )

    def test_pop_rejects_cumulative_fields(self) -> None:
        with pytest.raises(ValueError, match="must not have"):
            Metric(
                label="Bad",
                type=MetricType.PERIOD_OVER_PERIOD,
                expression="{[Revenue]}",
                period_over_period=PeriodOverPeriod(
                    time_dimension="D",
                    grain=TimeGrain.MONTH,
                    offset_grain=TimeGrain.YEAR,
                ),
                measure="Revenue",
            )

    def test_pop_comparison_defaults_to_percent_change(self) -> None:
        pop = PeriodOverPeriod(
            time_dimension="D",
            grain=TimeGrain.MONTH,
            offset_grain=TimeGrain.YEAR,
        )
        assert pop.comparison == PeriodOverPeriodComparison.PERCENT_CHANGE

    def test_pop_offset_defaults_to_minus_one(self) -> None:
        pop = PeriodOverPeriod(
            time_dimension="D",
            grain=TimeGrain.MONTH,
            offset_grain=TimeGrain.YEAR,
        )
        assert pop.offset == -1


# ── Resolution tests ──────────────────────────────────────────────────────


class TestPoPResolution:
    def test_resolve_pop_metric(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        resolved = resolver.resolve(query, model)
        assert resolved.has_pop
        pop_m = next(m for m in resolved.measures if m.name == "Revenue YoY Growth")
        assert pop_m.is_pop
        assert pop_m.pop_time_dimension == "Order Date"
        assert pop_m.pop_grain == TimeGrain.MONTH
        assert pop_m.pop_offset == -1
        assert pop_m.pop_offset_grain == TimeGrain.YEAR
        assert pop_m.pop_comparison == PeriodOverPeriodComparison.PERCENT_CHANGE
        assert "Revenue" in pop_m.component_measures

    def test_pop_unknown_time_dimension_error(self) -> None:
        yaml = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: DB
    schema: S
    columns:
      V:
        code: V
        abstractType: float
      D:
        code: D
        abstractType: date
dimensions:
  Dim:
    dataObject: T
    column: D
    resultType: date
measures:
  M:
    columns:
      - dataObject: T
        column: V
    aggregation: sum
metrics:
  Bad:
    type: period_over_period
    expression: '{[M]}'
    periodOverPeriod:
      timeDimension: NonExistent
      grain: month
      offsetGrain: year
"""
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(yaml)
        _model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        assert any("NonExistent" in e.message for e in result.errors)

    def test_pop_time_dim_not_in_select_error(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue YoY Growth"],
            ),
        )
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(query, model)
        assert any("POP_TIME_DIMENSION_NOT_IN_SELECT" in e.code for e in exc_info.value.errors)

    def test_has_pop_false_when_no_pop_metrics(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue"],
            ),
        )
        resolved = resolver.resolve(query, model)
        assert not resolved.has_pop


# ── Pipeline SQL generation tests ─────────────────────────────────────────


class TestPoPSQLGeneration:
    def test_pop_generates_4_ctes(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "DATE_RANGE" in sql
        assert "DATE_SPINE" in sql
        assert "POP_BASE" in sql
        assert "POP_COMPARE" in sql

    def test_pop_self_join_alias_avoids_reserved_word(self) -> None:
        """The self-join alias is ``pop_prev``, never the bare ``prev``.

        ``prev`` is a reserved word in Dremio and is rejected as an unquoted
        table alias, which broke period-over-period on the Dremio dialect.
        """
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        for dialect in ("dremio", "duckdb", "postgres", "snowflake"):
            sql = pipeline.compile(query, model, dialect).sql
            assert "pop_prev" in sql
            assert "AS prev" not in sql
            assert " prev." not in sql

    def test_pop_percent_change_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        # percentChange = current / NULLIF(prev, 0) - 1
        assert "NULLIF" in sql
        assert "- 1" in sql

    def test_pop_difference_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue MoM Diff"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        # difference = current - prev
        assert "pop_base" in sql.lower()

    def test_pop_previous_value_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue Prev Year"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "PREV" in sql

    def test_pop_with_dimension_filter(self) -> None:
        """Filters should be pushed into the date_range CTE."""
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Customer Country"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
            where=[
                QueryFilter(
                    field="Customer Country",
                    op=FilterOperator.EQ,
                    value="Germany",
                ),
            ],
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "DATE_RANGE" in sql
        assert "GERMANY" in sql

    def test_pop_non_pop_measures_preserved(self) -> None:
        """Non-PoP measures should pass through in the output."""
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        assert '"Revenue"' in sql or "Revenue" in sql

    def test_explain_has_pop(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        assert result.explain is not None
        assert result.explain.has_pop

    def test_explain_no_pop(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        assert result.explain is not None
        assert not result.explain.has_pop

    def test_no_pop_returns_ast_unchanged(self) -> None:
        """When no PoP metrics are present, wrap_with_pop returns the AST unchanged."""
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        # Should NOT contain PoP CTEs
        sql = result.sql.upper()
        assert "DATE_RANGE" not in sql
        assert "POP_COMPARE" not in sql


# ── Multi-dialect SQL generation tests ────────────────────────────────────


class TestPoPMultiDialect:
    @pytest.mark.parametrize(
        "dialect_name",
        ["duckdb", "postgres", "snowflake", "bigquery"],
    )
    def test_pop_compiles_per_dialect(self, dialect_name: str) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, dialect_name)
        sql = result.sql.upper()
        assert "DATE_RANGE" in sql
        assert "DATE_SPINE" in sql
        assert "POP_BASE" in sql
        assert "POP_COMPARE" in sql

    @pytest.mark.parametrize(
        "dialect_name",
        ["databricks", "mysql", "clickhouse", "dremio"],
    )
    def test_pop_compiles_remaining_dialects(self, dialect_name: str) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, dialect_name)
        sql = result.sql.upper()
        assert "DATE_RANGE" in sql
        assert "POP_COMPARE" in sql

    def test_pop_with_multiple_dimensions(self) -> None:
        """PoP with non-time dimensions should include them in the self-join."""
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Customer Country"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        sql = result.sql.upper()
        assert "POP_COMPARE" in sql
        # Should match non-time dims in the self-join
        assert "PREV" in sql


# ── Time dimension on a different table than measures ────────────────────

# Mimics TPC-H: Order Date on Orders, Revenue on Line Items
_POP_CROSS_TABLE_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: orders
    schema: main
    columns:
      Order Key:
        code: o_orderkey
        abstractType: int
      Order Date:
        code: o_orderdate
        abstractType: date

  Line Items:
    code: lineitem
    schema: main
    columns:
      Line Order Key:
        code: l_orderkey
        abstractType: int
      Extended Price:
        code: l_extendedprice
        abstractType: float
        numClass: additive
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom:
          - Line Order Key
        columnsTo:
          - Order Key

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date

measures:
  Revenue:
    columns:
      - dataObject: Line Items
        column: Extended Price
    resultType: float
    aggregation: sum

metrics:
  Revenue MoM:
    type: period_over_period
    expression: "{[Revenue]}"
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offsetGrain: month
      comparison: difference
"""


class TestPoPTimeDimOnDifferentTable:
    """PoP with time dimension on a different table than the measures.

    This is the TPC-H pattern: Order Date lives on 'Orders', but Revenue
    is aggregated from 'Line Items'. The pop_base CTE must:
    1. LEFT JOIN Orders onto the spine (via date truncation)
    2. LEFT JOIN Line Items onto Orders (via reversed FK)
    """

    def test_pop_cross_table_compiles(self) -> None:
        model = _load_model(_POP_CROSS_TABLE_YAML)
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue MoM"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        upper = sql.upper()

        # 4 CTEs present
        assert "DATE_RANGE" in upper
        assert "DATE_SPINE" in upper
        assert "POP_BASE" in upper
        assert "POP_COMPARE" in upper

        # pop_base joins Orders first (time dim table), then Line Items (fact)
        assert '"main"."orders"' in sql
        assert '"main"."lineitem"' in sql

        # Uses physical codes, not display names, in JOIN ON
        assert '"l_orderkey"' in sql
        assert '"o_orderkey"' in sql

    @pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake", "bigquery"])
    def test_pop_cross_table_all_dialects(self, dialect: str) -> None:
        model = _load_model(_POP_CROSS_TABLE_YAML)
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue MoM"],
            ),
        )
        result = pipeline.compile(query, model, dialect)
        assert result.sql_valid
        assert "pop_compare" in result.sql.lower()
