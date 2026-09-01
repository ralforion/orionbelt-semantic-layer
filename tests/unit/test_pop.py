"""Tests for period-over-period (PoP) metrics: model, resolution, wrapping, and SQL generation."""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
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
                name="Bad",
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
                name="Bad",
                type=MetricType.PERIOD_OVER_PERIOD,
                expression="{[Revenue]}",
            )

    def test_pop_rejects_cumulative_fields(self) -> None:
        with pytest.raises(ValueError, match="must not have"):
            Metric(
                name="Bad",
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

    def test_pop_mixed_offsets_get_separate_joins(self) -> None:
        """MoM + YoY in one query each get their own prior-period self-join.

        Both share the time dimension and base grain (month) but differ in
        comparison offset (1 month vs 1 year). The first offset is served by the
        spine's ``spine_date_prev``; the second gets its own ``pop_prev_1``
        self-join with the prior date computed inline.
        """
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue MoM Diff", "Revenue YoY Growth"],
            ),
        )
        sql = pipeline.compile(query, model, "duckdb").sql
        # A second, distinct prior-period self-join exists for the year offset.
        assert "pop_prev_1" in sql
        # The year offset is computed inline (not reusing the month spine_date_prev).
        assert "INTERVAL '-1 year'" in sql

    def test_pop_mixed_base_grain_rejected(self) -> None:
        """PoP metrics in one query must share the base grain (single spine)."""
        # Flip MoM Diff's base grain to quarter; this block (offsetGrain month +
        # comparison difference) is unique to MoM Diff in the fixture.
        yaml_txt = POP_MODEL_YAML.replace(
            "      timeDimension: Order Date\n"
            "      grain: month\n"
            "      offset: -1\n"
            "      offsetGrain: month\n"
            "      comparison: difference\n",
            "      timeDimension: Order Date\n"
            "      grain: quarter\n"
            "      offset: -1\n"
            "      offsetGrain: month\n"
            "      comparison: difference\n",
        )
        assert "grain: quarter" in yaml_txt  # guard: the replace actually fired
        raw, _ = TrackedLoader().load_string(yaml_txt)
        model, _ = ReferenceResolver().resolve(raw)
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue MoM Diff", "Revenue YoY Growth"],
            ),
        )
        with pytest.raises(ResolutionError, match="different time grains"):
            pipeline.compile(query, model, "duckdb")

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

    def test_pop_having_filter_is_applied(self) -> None:
        """A HAVING filter on a PoP metric must reach the compiled SQL.

        Regression: the PoP wrapper rebuilt the outer SELECT from scratch and
        dropped ``resolved.having_filters``, so a filter on a PoP metric was
        silently ignored (every row came back). The metric is a materialised
        column in ``pop_compare``, so the filter becomes a WHERE over that CTE.
        """

        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue YoY Growth"],
            ),
            having=[QueryFilter(field="Revenue YoY Growth", op=FilterOperator.GT, value=0)],
        )
        result = pipeline.compile(query, model, "duckdb")
        # The filter must land in the final SELECT over pop_compare as a WHERE.
        tail = result.sql.split("pop_compare")[-1]
        assert "Revenue YoY Growth" in tail
        assert "> 0" in tail
        assert "WHERE" in tail.upper()

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

    def test_the_mysql_bucket_carries_the_same_type_as_the_grain(self) -> None:
        """The PoP bucket and the dimension grain are the same truncation.

        The grain a dimension renders with is typed, but the spine's bucket is
        rendered by the string-level helper, which was still bare text: the
        same model answered ``Order Date`` as a date without a PoP metric and
        as a string with one.
        """
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
        )
        sql = CompilationPipeline().compile(query, model, "mysql").sql
        assert "DATE_FORMAT" in sql, "the format-string path is not exercised"
        for before in sql.split("DATE_FORMAT")[:-1]:
            assert before.endswith("CAST("), f"a bucket is projected as text: ...{before[-60:]}"

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


# ---------------------------------------------------------------------------
# Declared dataType on a PoP metric
# ---------------------------------------------------------------------------

_TYPED_POP_YAML = POP_MODEL_YAML.replace(
    """  Revenue YoY Growth:
    type: period_over_period
    expression: '{[Revenue]}'
""",
    """  Revenue YoY Growth:
    type: period_over_period
    expression: '{[Revenue]}'
    dataType: 'decimal(18, 4)'
""",
)


def _typed_model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(_TYPED_POP_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, [e.message for e in result.errors]
    return model


class TestPoPDeclaredDataType:
    """A PoP metric's ``dataType`` must reach the projection.

    ``pop_wrap`` was the only metric wrapper that never applied it, so the
    ratio carried whatever scale each engine's decimal division produced. For
    a metric declared ``decimal(18, 4)`` DuckDB returned 0.9931620307032472,
    BigQuery 0.993162031 and Snowflake 0.99316203 — three engines, three
    answers, none of them the declared 0.9932.
    """

    QUERY = QueryObject(
        select=QuerySelect(
            dimensions=["Order Date"],
            measures=["Revenue", "Revenue YoY Growth"],
        ),
    )

    def test_declared_type_is_applied(self) -> None:
        sql = CompilationPipeline().compile(self.QUERY, _typed_model(), "duckdb").sql
        assert 'CAST("Revenue YoY Growth" AS DECIMAL(18, 4))' in sql

    @pytest.mark.parametrize(
        "dialect,expected",
        [
            ("duckdb", 'CAST("Revenue YoY Growth" AS DECIMAL(18, 4))'),
            ("postgres", 'CAST("Revenue YoY Growth" AS DECIMAL(18, 4))'),
            ("snowflake", 'CAST("Revenue YoY Growth" AS NUMBER(18, 4))'),
        ],
    )
    def test_cast_is_dialect_idiomatic(self, dialect: str, expected: str) -> None:
        sql = CompilationPipeline().compile(self.QUERY, _typed_model(), dialect).sql
        assert expected in sql

    def test_cast_wraps_the_materialised_column_not_the_raw_ratio(self) -> None:
        """The cast belongs outside ``pop_compare``, over the finished value."""
        sql = CompilationPipeline().compile(self.QUERY, _typed_model(), "duckdb").sql
        compare_at = sql.index('"pop_compare" AS (')
        cast_at = sql.index('CAST("Revenue YoY Growth"')
        assert cast_at > compare_at
        # The division itself stays uncast inside the CTE.
        assert 'NULLIF(pop_prev."Revenue", 0) - 1 AS "Revenue YoY Growth"' in sql

    def test_having_filters_the_typed_value_not_the_raw_ratio(self) -> None:
        """The filter must see the same value the projection returns.

        Both live in one SELECT and WHERE is evaluated before the select list,
        so a bare alias reference reads ``pop_compare``'s *uncast* column. With
        a metric declared ``decimal(18, 4)`` that made `> 0.99318` drop a row
        whose returned value is 0.9932, because the underlying ratio is
        0.9931620307.
        """
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue YoY Growth"],
            ),
            having=[
                QueryFilter(
                    field="Revenue YoY Growth",
                    op=FilterOperator.GT,
                    value=0.99318,
                )
            ],
        )
        sql = CompilationPipeline().compile(query, _typed_model(), "duckdb").sql
        where = sql[sql.rindex("WHERE") :]
        assert 'CAST("Revenue YoY Growth" AS DECIMAL(18, 4)) > 0.99318' in where
        # The bare column must not be compared anywhere in the filter.
        assert '"Revenue YoY Growth" > 0.99318' not in where

    def test_having_on_a_non_pop_measure_is_untouched(self) -> None:
        """Only PoP metrics are cast in this projection, so only they are wrapped."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue YoY Growth"],
            ),
            having=[
                QueryFilter(field="Revenue", op=FilterOperator.GT, value=5),
            ],
        )
        sql = CompilationPipeline().compile(query, _typed_model(), "duckdb").sql
        where = sql[sql.rindex("WHERE") :]
        assert '"Revenue" > 5' in where
        assert 'CAST("Revenue" AS' not in where

    def test_undeclared_percent_change_takes_the_division_default(self) -> None:
        """A ratio is not a value in the base measure's units.

        Without an explicit ``dataType`` a PoP metric used to fall through to
        the model's default numeric type, which is ``decimal(18, 2)`` — two
        decimal places for a growth ratio. ``percentChange`` and ``ratio``
        divide, so they take the same ``decimal(18, 6)`` default that an
        expression containing ``/`` already gets.
        """
        sql = CompilationPipeline().compile(self.QUERY, _load_model(), "duckdb").sql
        assert 'CAST("Revenue YoY Growth" AS DECIMAL(18, 6))' in sql

    def test_difference_inherits_the_base_measure_type(self) -> None:
        """``difference`` carries the measure's own units, so no ratio default.

        The inheritance is real rather than nominal: ``pop_base`` applies the
        base measure's declared cast, so the subtraction is over typed
        operands. It used to emit a bare ``SUM(...)`` there, which left the
        comparison with no type to inherit at all.
        """
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Revenue MoM Diff"],
            ),
        )
        sql = CompilationPipeline().compile(query, _load_model(), "duckdb").sql
        assert 'CAST("Revenue MoM Diff"' not in sql
        # The operand it inherits from is typed. It reads the column out of the
        # derived table ``pop_base`` selects from, so the reference is flat.
        assert 'CAST(SUM("__ob_pop_src"."Orders__AMOUNT") AS DECIMAL(18, 2)) AS "Revenue"' in sql

    def test_a_wrapper_metric_placeholder_is_not_cast_early(self) -> None:
        """A window metric's ``pop_base`` column is the base measure, not the rank.

        It only holds the base aggregate until ``window_wrap`` builds the real
        window call, so the metric's own dataType does not describe it yet.
        Casting early corrupts the input: a rank declaring ``dataType: integer``
        truncated ``SUM(amount)`` to INT, so 1.49 and 1.40 both became 1 and
        ranked equal. The finished window value is still cast, by window_wrap.
        """
        raw, source_map = TrackedLoader().load_string(_RANK_POP_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, [e.message for e in result.errors]
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Day", "Region"],
                measures=["Region Rank", "Amount MoM"],
            ),
        )
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        pop_base = sql[sql.index('"pop_base" AS (') : sql.index('"pop_compare" AS (')]
        assert 'SUM("__ob_pop_src"."Sales__amount") AS "Region Rank"' in pop_base
        assert "CAST" not in pop_base.split('AS "Region Rank"')[0].rsplit(",", 1)[-1]
        # The window result itself still carries the metric's declared type.
        assert 'CAST(RANK() OVER (ORDER BY "Amount Sum" DESC) AS INTEGER)' in sql

    def test_a_measure_keeps_its_declared_type_inside_a_pop_query(self) -> None:
        """``pop_base`` rebuilds the aggregation, so it must re-apply the cast.

        Without it the same measure had one type in a plain query and another
        in a PoP query, and every comparison built on it inherited the untyped
        form.
        """
        plain = (
            CompilationPipeline()
            .compile(
                QueryObject(
                    select=QuerySelect(dimensions=["Order Date"], measures=["Revenue"]),
                ),
                _load_model(),
                "duckdb",
            )
            .sql
        )
        pop = (
            CompilationPipeline()
            .compile(
                QueryObject(
                    select=QuerySelect(
                        dimensions=["Order Date"], measures=["Revenue", "Revenue YoY Growth"]
                    ),
                ),
                _load_model(),
                "duckdb",
            )
            .sql
        )
        # Same aggregate and same declared type in both. The source differs by
        # construction: a plain query reads the fact table, and ``pop_base``
        # reads the derived table it joins the date spine to.
        assert 'CAST(SUM("Orders"."AMOUNT") AS DECIMAL(18, 2)) AS "Revenue"' in plain
        assert 'CAST(SUM("__ob_pop_src"."Orders__AMOUNT") AS DECIMAL(18, 2)) AS "Revenue"' in pop


_RANK_POP_YAML = """\
version: 1.0
dataObjects:
  Sales:
    code: sales
    schema: main
    columns:
      Id: {code: id, abstractType: string, primaryKey: true}
      Amount: {code: amount, abstractType: float}
      Day: {code: day, abstractType: date}
      Region: {code: region, abstractType: string}
dimensions:
  Day: {dataObject: Sales, column: Day}
  Region: {dataObject: Sales, column: Region}
measures:
  Amount Sum:
    aggregation: sum
    dataType: "decimal(18, 2)"
    expression: '{[Sales].[Amount]}'
metrics:
  Region Rank:
    type: window
    measure: Amount Sum
    windowFunction: rank
    orderDirection: desc
    dataType: integer
  Amount MoM:
    type: period_over_period
    expression: '{[Amount Sum]}'
    dataType: "decimal(18, 2)"
    periodOverPeriod:
      timeDimension: Day
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


class TestPopMultiFact:
    """A PoP metric in a query the CFL planner unioned is refused, not compiled (#366).

    ``wrap_with_pop`` does not read the plan it wraps: it rebuilds a FROM of its
    own around a date spine, and that shape holds one join tree. Given two
    independent facts it applied every leg's joins to every leg and left the
    composite CTE declared and never referenced, so the database rejected the
    statement by naming a data object from the model.
    """

    MODEL_YAML = """
version: 1.0
name: pop_multi_fact
dataObjects:
  Calendar:
    code: calendar
    columns:
      Day: {code: day, abstractType: date}
  Sales:
    code: sales
    joins: [{joinTo: Calendar, columnsFrom: [Sold On], columnsTo: [Day], joinType: many-to-one}]
    columns:
      Sold On: {code: sold_on, abstractType: date}
      Amount:  {code: amount, abstractType: float, numClass: additive}
  Returns:
    code: returns
    joins: [{joinTo: Calendar, columnsFrom: [Returned On], columnsTo: [Day], joinType: many-to-one}]
    columns:
      Returned On: {code: returned_on, abstractType: date}
      Refund:      {code: refund, abstractType: float, numClass: additive}
dimensions:
  Day Month: {dataObject: Calendar, column: Day, resultType: date, timeGrain: month}
measures:
  Sales Total:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Refund Total:
    columns: [{dataObject: Returns, column: Refund}]
    resultType: float
    aggregation: sum
metrics:
  Sales MoM:
    type: period_over_period
    expression: '{[Sales Total]}'
    periodOverPeriod:
      timeDimension: Day Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""

    def _model(self) -> SemanticModel:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def _compile(self, measures: list[str]) -> CompilationResult:
        query = QueryObject(
            select=QuerySelect(dimensions=["Day Month"], measures=measures),
        )
        return CompilationPipeline().compile(query, self._model(), "duckdb")

    def test_measures_from_independent_facts_are_refused(self) -> None:
        with pytest.raises(ResolutionError) as excinfo:
            self._compile(["Sales Total", "Refund Total", "Sales MoM"])
        (error,) = excinfo.value.errors
        assert error.code == "INVALID_METRIC"
        assert error.context["factTables"] == ["Returns", "Sales"]
        assert error.context["compositeCte"] == "composite_01"

    def test_the_metrics_own_fact_still_compiles(self) -> None:
        """The CFL planner delegates back to star here, so there is a join tree.

        The refusal reads ``composite_cte``, which is set only when a union was
        actually produced, so this query is untouched by it.
        """
        result = self._compile(["Sales Total", "Sales MoM"])
        assert "composite_01" not in result.sql
        assert "date_spine" in result.sql


class TestPopSourceColumnTypes:
    """The declared abstractType survives the derived table (#356 / #361 / #362).

    ``pop_base`` reads its columns out of a subquery, so every reference in it
    is rebuilt. A rebuilt one that dropped the column's declared type left
    ClickHouse unable to see that it was looking at a number, and its guard
    against an overflowing integer cast is exactly that question: a measure was
    guarded in a plain query and unguarded in the same query with a PoP metric
    in it, where the engine wraps rather than raises.
    """

    MODEL_YAML = """
version: 1.0
name: pop_source_types
dataObjects:
  Event:
    code: event
    columns:
      Occurred: {code: occurred, abstractType: date}
      Big:      {code: big, abstractType: int}
dimensions:
  Occurred Month:
    dataObject: Event
    column: Occurred
    resultType: date
    timeGrain: month
measures:
  Big Max:
    columns: [{dataObject: Event, column: Big}]
    aggregation: max
    resultType: int
    dataType: "integer"
metrics:
  Big MoM:
    type: period_over_period
    expression: '{[Big Max]}'
    periodOverPeriod:
      timeDimension: Occurred Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""

    def _sql(self, measures: list[str]) -> str:
        raw, source_map = TrackedLoader().load_string(self.MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(dimensions=["Occurred Month"], measures=measures),
        )
        return CompilationPipeline().compile(query, model, "clickhouse").sql

    def test_clickhouse_guards_the_narrowing_cast_inside_a_pop_query(self) -> None:
        """``MAX`` over an int is numeric wherever the reference is read from."""
        assert 'accurateCast(trunc(MAX("__ob_pop_src"."Event__big"))' in self._sql(
            ["Big Max", "Big MoM"]
        )

    def test_the_plain_query_is_the_same_shape(self) -> None:
        """The guard is not new here; what is new is that it survives the rewrite."""
        assert 'accurateCast(trunc(MAX("Event"."big"))' in self._sql(["Big Max"])


class TestPopFilters:
    """A ``where`` filter reaches the measures, not only the spine's range (#365).

    ``date_range`` pushed the filters down and ``pop_base`` had no WHERE at all,
    so the spine covered the filtered extent while every measure aggregated
    every row. Nothing failed: the query returned a well-formed result with the
    wrong numbers in it, and adding a period-over-period metric to an existing
    filtered query silently changed what its other measures meant.
    """

    MODEL_YAML = """
version: 1.0
name: pop_filters
dataObjects:
  Event:
    code: event
    columns:
      Occurred: {code: occurred, abstractType: date}
      Region:   {code: region, abstractType: string}
      Amount:   {code: amount, abstractType: float, numClass: additive}
dimensions:
  Occurred Month:
    dataObject: Event
    column: Occurred
    resultType: date
    timeGrain: month
  Region: {dataObject: Event, column: Region, resultType: string}
measures:
  Total:
    columns: [{dataObject: Event, column: Amount}]
    resultType: float
    aggregation: sum
metrics:
  Total MoM:
    type: period_over_period
    expression: '{[Total]}'
    periodOverPeriod:
      timeDimension: Occurred Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""

    def _model(self) -> SemanticModel:
        raw, source_map = TrackedLoader().load_string(self.MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def _rows(self, measures: list[str]) -> list[tuple]:
        query = QueryObject(
            select=QuerySelect(dimensions=["Occurred Month"], measures=measures),
            where=[QueryFilter(field="Region", op=FilterOperator.EQ, value="EU")],
        )
        sql = CompilationPipeline().compile(query, self._model(), "duckdb").sql
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE event AS SELECT * FROM (VALUES"
            " (DATE '2024-01-10', 'EU', 10.0),"
            " (DATE '2024-01-11', 'US', 700.0),"
            " (DATE '2024-02-10', 'EU', 20.0)"
            ") t(occurred, region, amount)"
        )
        return sorted(con.execute(sql).fetchall(), key=lambda row: row[0])

    def test_the_measure_reads_the_filtered_rows(self) -> None:
        """January is 10, not 710. The 700 belongs to the region the query excluded."""
        rows = self._rows(["Total", "Total MoM"])
        assert [(row[0].isoformat(), row[1]) for row in rows] == [
            ("2024-01-01", Decimal("10.00")),
            ("2024-02-01", Decimal("20.00")),
        ]

    def test_the_metric_agrees_with_the_measure_it_compares(self) -> None:
        """February minus January over the filtered rows is +10, not -690."""
        rows = self._rows(["Total", "Total MoM"])
        assert rows[1][2] == Decimal("10.00")

    def test_the_same_query_without_the_metric_gives_the_same_measure(self) -> None:
        """The defect was visible as a disagreement between these two queries.

        Compared on the period's ISO date rather than the value itself: the
        spine hands back a DATE and ``DATE_TRUNC`` a TIMESTAMP, which is a
        separate difference between the two paths and not what this is about.
        """
        with_metric = [
            (row[0].isoformat()[:10], row[1]) for row in self._rows(["Total", "Total MoM"])
        ]
        without = [(row[0].isoformat()[:10], row[1]) for row in self._rows(["Total"])]
        assert with_metric == without
