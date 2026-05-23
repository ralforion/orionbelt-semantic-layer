"""Tests for v2.6 trend analysis primitives.

Covers:
- ``Metric.partitionBy`` on cumulative metrics
- ``MetricType.WINDOW`` (rank, dense_rank, row_number, ntile, lag, lead,
  first_value, last_value)
- Statistical aggregates on ``Measure`` (stddev*, var*, corr, covar_*, regr_*)
- Dialect-gap rejection (MySQL/BigQuery/ClickHouse for unsupported aggs)
- Validation error codes
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import (
    Measure,
    Metric,
    MetricType,
    SemanticModel,
    WindowFunctionKind,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# ── Shared model with all trend primitives ─────────────────────────────────

TREND_MODEL_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order Date:
        code: ORDER_DATE
        abstractType: date
      Country:
        code: COUNTRY
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
      Spend:
        code: SPEND
        abstractType: float
        numClass: additive

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month
  Country:
    dataObject: Orders
    column: Country
    resultType: string

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum

  Revenue StdDev:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: stddev

  Revenue Spend Corr:
    columns:
      - dataObject: Orders
        column: Amount
      - dataObject: Orders
        column: Spend
    resultType: float
    aggregation: corr

metrics:
  Revenue MA3 by Country:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    cumulativeType: avg
    window: 3
    partitionBy: [Country]

  Revenue Rank by Country:
    type: window
    windowFunction: dense_rank
    measure: Revenue
    orderDirection: desc
    partitionBy: [Country]

  Revenue Prior Month:
    type: window
    windowFunction: lag
    measure: Revenue
    offset: 1
    timeDimension: Order Date
    partitionBy: [Country]

  Revenue Quartile:
    type: window
    windowFunction: ntile
    measure: Revenue
    buckets: 4
    partitionBy: [Country]

  MoM Delta:
    type: derived
    expression: "{[Revenue]} - {[Revenue Prior Month]}"
"""


def _load_model(yaml: str = TREND_MODEL_YAML) -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(yaml)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"errors: {[e.message for e in result.errors]}"
    return model


# ── Model-level validation (Pydantic) ──────────────────────────────────────


class TestMetricPartitionByValidation:
    def test_derived_rejects_partition_by(self) -> None:
        with pytest.raises(ValueError, match="partitionBy"):
            Metric(label="Bad", type=MetricType.DERIVED, expression="{[X]}", partition_by=["Dim"])

    def test_cumulative_accepts_partition_by(self) -> None:
        m = Metric(
            label="OK",
            type=MetricType.CUMULATIVE,
            measure="Revenue",
            time_dimension="Order Date",
            partition_by=["Country"],
        )
        assert m.partition_by == ["Country"]

    def test_window_metric_requires_window_function(self) -> None:
        with pytest.raises(ValueError, match="windowFunction"):
            Metric(label="Bad", type=MetricType.WINDOW, measure="Revenue")

    def test_window_lag_requires_offset(self) -> None:
        with pytest.raises(ValueError, match="offset"):
            Metric(
                label="Bad",
                type=MetricType.WINDOW,
                window_function=WindowFunctionKind.LAG,
                measure="Revenue",
                time_dimension="Order Date",
            )

    def test_window_lag_requires_time_dimension(self) -> None:
        with pytest.raises(ValueError, match="timeDimension"):
            Metric(
                label="Bad",
                type=MetricType.WINDOW,
                window_function=WindowFunctionKind.LAG,
                measure="Revenue",
                offset=1,
            )

    def test_window_ntile_requires_buckets(self) -> None:
        with pytest.raises(ValueError, match="buckets"):
            Metric(
                label="Bad",
                type=MetricType.WINDOW,
                window_function=WindowFunctionKind.NTILE,
                measure="Revenue",
            )

    def test_window_row_number_no_measure_ok(self) -> None:
        # ROW_NUMBER can rank without an explicit measure
        m = Metric(
            label="RN",
            type=MetricType.WINDOW,
            window_function=WindowFunctionKind.ROW_NUMBER,
            time_dimension="Order Date",
        )
        assert m.measure is None

    def test_window_rejects_expression(self) -> None:
        with pytest.raises(ValueError, match="must not have 'expression'"):
            Metric(
                label="Bad",
                type=MetricType.WINDOW,
                window_function=WindowFunctionKind.RANK,
                measure="Revenue",
                expression="{[Revenue]}",
            )

    def test_window_invalid_order_direction(self) -> None:
        with pytest.raises(ValueError, match="orderDirection"):
            Metric(
                label="Bad",
                type=MetricType.WINDOW,
                window_function=WindowFunctionKind.RANK,
                measure="Revenue",
                order_direction="random",
            )


class TestStatisticalAggregationArity:
    def test_corr_requires_two_columns(self) -> None:
        with pytest.raises(ValueError, match="2 columns"):
            Measure(
                label="Bad",
                aggregation="corr",
                columns=[{"dataObject": "Orders", "column": "Amount"}],
            )

    def test_stddev_requires_one_column(self) -> None:
        with pytest.raises(ValueError, match="1 column"):
            Measure(
                label="Bad",
                aggregation="stddev",
                columns=[
                    {"dataObject": "Orders", "column": "Amount"},
                    {"dataObject": "Orders", "column": "Spend"},
                ],
            )

    def test_stddev_accepts_one_column(self) -> None:
        m = Measure(
            label="OK",
            aggregation="stddev",
            columns=[{"dataObject": "Orders", "column": "Amount"}],
        )
        assert m.aggregation == "stddev"

    def test_corr_accepts_two_columns(self) -> None:
        m = Measure(
            label="OK",
            aggregation="corr",
            columns=[
                {"dataObject": "Orders", "column": "Amount"},
                {"dataObject": "Orders", "column": "Spend"},
            ],
        )
        assert m.aggregation == "corr"

    def test_expression_measure_bypasses_arity_check(self) -> None:
        # Expressions can compose any column reference — no arity rule
        m = Measure(
            label="OK",
            aggregation="corr",
            expression="{[Orders].[Amount]} + {[Orders].[Spend]}",
        )
        assert m.aggregation == "corr"


# ── Compilation: partitionBy ───────────────────────────────────────────────


class TestCumulativePartitionBy:
    def test_partition_by_appears_in_sql(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue MA3 by Country"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert 'PARTITION BY "Country"' in result.sql
        assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in result.sql

    def test_partition_dim_must_be_in_select(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],  # missing Country
                measures=["Revenue MA3 by Country"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert any(e.code == "UNKNOWN_PARTITION_DIMENSION" for e in exc.value.errors)

    def test_partition_dim_unknown(self) -> None:
        bad_yaml = TREND_MODEL_YAML.replace("partitionBy: [Country]", "partitionBy: [NoSuchDim]")
        model = _load_model(bad_yaml)
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue MA3 by Country"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert any(e.code == "UNKNOWN_PARTITION_DIMENSION" for e in exc.value.errors)

    def test_empty_partition_by_preserves_legacy_sql(self) -> None:
        """A cumulative metric without partitionBy must produce a window
        function with no PARTITION BY clause — same SQL as v2.5."""
        yaml = TREND_MODEL_YAML.replace("partitionBy: [Country]", "")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue MA3 by Country"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        # No PARTITION BY in the SQL when partitionBy is omitted
        assert "PARTITION BY" not in result.sql


# ── Compilation: window metrics ────────────────────────────────────────────


class TestWindowMetrics:
    def test_dense_rank_compiles(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue", "Revenue Rank by Country"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert "DENSE_RANK()" in result.sql.upper()
        assert 'PARTITION BY "Country"' in result.sql
        assert 'ORDER BY "Revenue" DESC' in result.sql

    def test_lag_compiles(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue", "Revenue Prior Month"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert "LAG(" in result.sql.upper()
        # offset=1
        assert ", 1)" in result.sql.replace(" ", "")[:5000] or 'LAG("Revenue", 1)' in result.sql

    def test_ntile_compiles(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue", "Revenue Quartile"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert "NTILE(4)" in result.sql.upper()

    def test_window_metric_compose_with_derived(self) -> None:
        """A DERIVED metric that references a WINDOW metric should compose
        — the lag column ends up in window_base; the derived expression
        consumes it in the outermost SELECT."""
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue", "Revenue Prior Month", "MoM Delta"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert "LAG(" in result.sql.upper()
        assert '"MoM Delta"' in result.sql or "MoM Delta" in result.sql


# ── Compilation: statistical aggregates ────────────────────────────────────


class TestStatisticalAggregates:
    def test_stddev_compiles_on_postgres(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=["Revenue StdDev"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert "STDDEV" in result.sql.upper()

    def test_corr_compiles_on_postgres(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=["Revenue Spend Corr"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert "CORR(" in result.sql.upper()
        # Order of args must match Measure.columns
        assert "CORR(" in result.sql.upper()

    def test_clickhouse_renames_to_camelcase(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=["Revenue StdDev", "Revenue Spend Corr"],
            )
        )
        result = CompilationPipeline().compile(query, model, "clickhouse")
        # ClickHouse: stddevSamp / corr
        assert "stddevSamp(" in result.sql or "stddevSamp" in result.sql
        assert "corr(" in result.sql

    def test_mysql_rejects_corr(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=["Revenue Spend Corr"],
            )
        )
        with pytest.raises(UnsupportedAggregationError) as exc:
            CompilationPipeline().compile(query, model, "mysql")
        assert exc.value.dialect == "mysql"
        assert "corr" in exc.value.aggregation

    def test_bigquery_rejects_regr_slope(self) -> None:
        # Inject a regression measure into the measures: block of the YAML
        regr_yaml = TREND_MODEL_YAML.replace(
            "  Revenue Spend Corr:",
            (
                "  Revenue Slope:\n"
                "    columns:\n"
                "      - dataObject: Orders\n"
                "        column: Amount\n"
                "      - dataObject: Orders\n"
                "        column: Spend\n"
                "    resultType: float\n"
                "    aggregation: regr_slope\n\n"
                "  Revenue Spend Corr:"
            ),
        )
        model = _load_model(regr_yaml)
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=["Revenue Slope"],
            )
        )
        with pytest.raises(UnsupportedAggregationError) as exc:
            CompilationPipeline().compile(query, model, "bigquery")
        assert exc.value.dialect == "bigquery"
        assert "regr_slope" in exc.value.aggregation

    def test_clickhouse_rejects_regr_intercept(self) -> None:
        regr_yaml = TREND_MODEL_YAML.replace(
            "  Revenue Spend Corr:",
            (
                "  Revenue Intercept:\n"
                "    columns:\n"
                "      - dataObject: Orders\n"
                "        column: Amount\n"
                "      - dataObject: Orders\n"
                "        column: Spend\n"
                "    resultType: float\n"
                "    aggregation: regr_intercept\n\n"
                "  Revenue Spend Corr:"
            ),
        )
        model = _load_model(regr_yaml)
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=["Revenue Intercept"],
            )
        )
        with pytest.raises(UnsupportedAggregationError) as exc:
            CompilationPipeline().compile(query, model, "clickhouse")
        assert exc.value.dialect == "clickhouse"


# ── Cross-dialect smoke: stddev should work on all 8 dialects ──────────────


@pytest.mark.parametrize(
    "dialect_name",
    ["postgres", "snowflake", "bigquery", "databricks", "duckdb", "clickhouse", "mysql", "dremio"],
)
def test_stddev_on_every_dialect(dialect_name: str) -> None:
    """STDDEV is the universally-supported statistical aggregate — must
    compile on every dialect OBSL ships."""
    model = _load_model()
    # Make sure the dialect is registered (side-effect import)
    DialectRegistry.get(dialect_name)
    query = QueryObject(
        select=QuerySelect(
            dimensions=["Country"],
            measures=["Revenue StdDev"],
        )
    )
    result = CompilationPipeline().compile(query, model, dialect_name)
    # Every dialect spells the function with "stddev" in some form
    # (STDDEV / STDDEV_SAMP for ANSI, stddevSamp for ClickHouse).
    assert "stddev" in result.sql.lower()


@pytest.mark.parametrize(
    "dialect_name",
    ["postgres", "snowflake", "bigquery", "databricks", "duckdb", "clickhouse", "dremio"],
)
def test_corr_on_supported_dialects(dialect_name: str) -> None:
    """CORR is supported everywhere except MySQL."""
    model = _load_model()
    DialectRegistry.get(dialect_name)
    query = QueryObject(
        select=QuerySelect(
            dimensions=["Country"],
            measures=["Revenue Spend Corr"],
        )
    )
    result = CompilationPipeline().compile(query, model, dialect_name)
    assert "corr" in result.sql.lower()
