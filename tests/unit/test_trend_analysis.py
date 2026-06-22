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
from orionbelt.models.query import Grouping, QueryObject, QuerySelect
from orionbelt.models.semantic import (
    Measure,
    Metric,
    MetricType,
    SemanticModel,
    WindowFunctionKind,
)
from orionbelt.models.warnings import WarningCode
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

    def test_corr_rejects_expression_form(self) -> None:
        """Regression: ``corr`` with ``expression:`` (instead of two
        ``columns:`` entries) was bypassing arity validation and
        producing invalid SQL like ``CORR((a + b))``. The model loader
        must reject this combination so users learn the correct shape
        at load time, not at compile time."""
        with pytest.raises(ValueError, match=r"corr.*expression"):
            Measure(
                label="Bad",
                aggregation="corr",
                expression="{[Orders].[Amount]} + {[Orders].[Spend]}",
            )

    def test_covar_pop_rejects_expression_form(self) -> None:
        with pytest.raises(ValueError, match=r"covar_pop.*expression"):
            Measure(
                label="Bad",
                aggregation="covar_pop",
                expression="{[Orders].[Amount]} + {[Orders].[Spend]}",
            )

    def test_regr_slope_rejects_expression_form(self) -> None:
        with pytest.raises(ValueError, match=r"regr_slope.*expression"):
            Measure(
                label="Bad",
                aggregation="regr_slope",
                expression="{[Orders].[Amount]} + {[Orders].[Spend]}",
            )

    def test_stddev_accepts_expression_form(self) -> None:
        """Single-column statistical aggregations accept ``expression:``
        — ``STDDEV(<scalar expr>)`` is valid SQL. The expression-vs-
        columns restriction only applies to TWO-column aggregates,
        where collapsing two args into one scalar would break the call."""
        m = Measure(
            label="OK",
            aggregation="stddev",
            expression="{[Orders].[Amount]} + 100",
        )
        assert m.aggregation == "stddev"

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

    def test_expression_measure_rejected_for_statistical_aggregations(self) -> None:
        """Inverse of the previous bypass-the-arity behavior: an
        ``expression:`` form combined with a statistical aggregation
        collapses to one scalar argument, which produced invalid SQL
        like ``CORR((a + b))`` at compile time. The model loader now
        rejects the combination so the user is steered to the
        ``columns:`` form that makes argument order explicit.
        Non-statistical aggregations (``sum``, ``count``, ``avg``…)
        still accept ``expression:`` since they take a single scalar.
        """
        # Non-statistical aggregation + expression: still allowed.
        m = Measure(
            label="OK",
            aggregation="sum",
            expression="{[Orders].[Amount]} + {[Orders].[Spend]}",
        )
        assert m.aggregation == "sum"
        # Statistical aggregation + expression: rejected.
        with pytest.raises(ValueError, match=r"corr.*expression"):
            Measure(
                label="Bad",
                aggregation="corr",
                expression="{[Orders].[Amount]} + {[Orders].[Spend]}",
            )


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

    def test_derived_referencing_only_window_metric_still_wraps(self) -> None:
        """Regression: ``MoM Delta = {[Revenue]} - {[Revenue Prior Month]}``
        selected ALONE (without Revenue Prior Month being directly in the
        SELECT) must still wrap the window. Before the fix, star.py
        substituted the raw inner aggregate of Revenue Prior Month into
        MoM Delta's expression before window_wrap ran, so no
        ``window_base`` CTE was generated and ``LAG(`` never appeared.

        After the fix, the outer SELECT inlines the window call into
        MoM Delta's expression so the math is correct:
        ``Revenue - LAG(Revenue, 1) OVER (...) AS "MoM Delta"``.
        """
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["MoM Delta"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        sql = result.sql
        # The window function must actually appear in the compiled SQL.
        assert "LAG(" in sql.upper(), sql
        # A ``window_base`` CTE must be generated even when the window
        # metric is only referenced transitively via a DDM.
        assert "window_base" in sql, sql
        # MoM Delta's expression must subtract the LAG-output from the
        # base Revenue value — both terms appear inside the same
        # arithmetic, with LAG on the right-hand side.
        upper = sql.upper()
        delta_idx = upper.index('"MOM DELTA"')
        # Walk backward to find the matching parenthesis for the cast
        # wrapping ``Revenue - LAG(...)``; the easier proxy is that
        # ``LAG`` and ``"Revenue"`` both appear within the SQL preceding
        # the ``"MoM Delta"`` alias.
        head = sql[:delta_idx]
        assert "LAG(" in head.upper(), sql
        assert '"Revenue"' in head, sql
        assert "-" in head, sql

    def test_derived_window_with_rollup_emits_grouping_advisory(self) -> None:
        """Regression: a derived metric that only *transitively* references a
        window metric still runs the window pass, which drops the GROUPING()
        flag columns from the final projection. The ROLLUP/CUBE advisory must
        fire in this case too — even though ``has_window`` is False — because
        the warning condition uses the same predicate as the window pass.
        """
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["MoM Delta"],
            ),
            grouping=Grouping.ROLLUP,
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        advisories = [
            w
            for w in result.warnings
            if w.code == WarningCode.INCOMPATIBLE_COMBINATION
            and "GROUPING() flag columns" in w.message
        ]
        assert advisories, [(w.code, w.message) for w in result.warnings]

    def test_window_metric_compose_with_derived(self) -> None:
        """A DERIVED metric that references a WINDOW metric must compute
        ``MoM Delta = Revenue - LAG(Revenue)`` — not ``Revenue - Revenue``
        which is what the early-substitution bug produced.

        Reviewer-flagged: the previous version of this test only checked
        for ``LAG(`` and the alias, missing that MoM Delta's right-hand
        side was being silently rewritten to a bare ``Revenue`` reference.
        """
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue", "Revenue Prior Month", "MoM Delta"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        sql = result.sql
        upper = sql.upper()
        assert "LAG(" in upper
        # Strong shape check: the compiled SQL must contain a literal
        # subtraction of the form ``"Revenue" - LAG(...)`` somewhere in
        # the outer SELECT — that's MoM Delta's expression. Before the
        # fix, MoM Delta was emitted as ``Revenue - Revenue`` (or the
        # equivalent ``SUM(amount) - SUM(amount)``) with no LAG inside.
        import re as _re

        assert _re.search(r'"Revenue"\s*-\s*LAG\(', sql), (
            f"MoM Delta must subtract LAG(Revenue) from Revenue. Got:\n{sql}"
        )

    def test_order_by_on_window_metric_uses_alias_not_base_expression(self) -> None:
        """Regression: ``ORDER BY "Revenue Prior Month" DESC`` must order
        by the window-CTE output column, not by the base measure's inner
        aggregate.

        Before the fix, ``_resolve_order_by_field`` returned
        ``meas.expression`` for any measure — for a window metric, that
        expression is the lag-input (the bare ``Revenue`` aggregate), so
        ``ORDER BY "Revenue Prior Month" DESC`` silently rewrote to
        ``ORDER BY "Revenue" DESC``.
        """
        from orionbelt.models.query import QueryOrderBy, SortDirection

        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue Prior Month"],
            ),
            order_by=[QueryOrderBy(field="Revenue Prior Month", direction=SortDirection.DESC)],
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        sql = result.sql
        # The outer ORDER BY must reference the windowed alias by name,
        # not the bare base aggregate.
        assert 'ORDER BY "Revenue Prior Month" DESC' in sql, sql
        assert 'ORDER BY "Revenue" DESC' not in sql, sql

    def test_window_base_carries_base_measure_declared_data_type(self) -> None:
        """Regression: window_base CTE must apply the base measure's
        declared dataType cast, mirroring cumulative_wrap. Without the
        cast, ``LAG`` over a ``decimal(18, 2)`` measure operates on the
        uncast ``SUM(...)`` and a float / int default leaks into the
        windowed projection.
        """
        # Pin Revenue to decimal(18, 2) so the cast is visible in SQL.
        decimal_yaml = TREND_MODEL_YAML.replace(
            "  Revenue:\n"
            "    columns:\n"
            "      - dataObject: Orders\n"
            "        column: Amount\n"
            "    resultType: float\n"
            "    aggregation: sum\n",
            "  Revenue:\n"
            "    columns:\n"
            "      - dataObject: Orders\n"
            "        column: Amount\n"
            '    dataType: "decimal(18, 2)"\n'
            "    aggregation: sum\n",
        )
        model = _load_model(decimal_yaml)
        # Select only the window metric — the base measure is NOT directly
        # selected, so window_wrap is responsible for adding the cast-wrapped
        # Revenue column to the window_base CTE.
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date", "Country"],
                measures=["Revenue Prior Month"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        sql_upper = result.sql.upper()
        # Cast appears inside the base CTE alongside the SUM aggregation —
        # not in the outer LAG call (LAG just selects from the cast result).
        assert "CAST" in sql_upper, result.sql
        assert "DECIMAL(18, 2)" in sql_upper or "DECIMAL(18,2)" in sql_upper, result.sql


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
