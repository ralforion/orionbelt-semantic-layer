"""Tests for cumulative metrics: model, resolution, wrapping, and SQL generation."""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import (
    AliasedExpr,
    ColumnRef,
    From,
    FunctionCall,
    Literal,
    OrderByItem,
    Select,
    WindowFunction,
)
from orionbelt.compiler.cumulative_wrap import wrap_with_cumulative
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import (
    QueryResolver,
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
)
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import (
    CumulativeAggType,
    GrainToDate,
    Metric,
    MetricType,
    SemanticModel,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# ── OBML YAML with cumulative metrics ──────────────────────────────────────

CUMULATIVE_MODEL_YAML = """\
version: 1.0

dataObjects:
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
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month

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
  # Derived (existing type)
  Revenue per Order:
    expression: '{[Revenue]} / {[Order Count]}'

  # Cumulative: running total (unbounded)
  Cumulative Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    description: Running total of revenue

  # Cumulative: rolling 7-period average
  7-Day Rolling Avg Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    cumulativeType: avg
    window: 7
    description: Trailing 7-day average revenue

  # Cumulative: month-to-date
  MTD Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    grainToDate: month
    description: Revenue from start of each month

  # Cumulative: year-to-date
  YTD Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    grainToDate: year

  # Cumulative: rolling max
  30-Day Peak Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    cumulativeType: max
    window: 30
"""


def _load_model(yaml_content: str = CUMULATIVE_MODEL_YAML) -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(yaml_content)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Model errors: {[e.message for e in result.errors]}"
    return model


# ── Model parsing tests ────────────────────────────────────────────────────


class TestMetricModel:
    def test_derived_metric_unchanged(self) -> None:
        model = _load_model()
        m = model.metrics["Revenue per Order"]
        assert m.type == MetricType.DERIVED
        assert m.expression == "{[Revenue]} / {[Order Count]}"
        assert m.measure is None

    def test_cumulative_metric_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["Cumulative Revenue"]
        assert m.type == MetricType.CUMULATIVE
        assert m.measure == "Revenue"
        assert m.time_dimension == "Order Date"
        assert m.cumulative_type == CumulativeAggType.SUM
        assert m.window is None
        assert m.grain_to_date is None

    def test_rolling_window_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["7-Day Rolling Avg Revenue"]
        assert m.type == MetricType.CUMULATIVE
        assert m.cumulative_type == CumulativeAggType.AVG
        assert m.window == 7
        assert m.grain_to_date is None

    def test_grain_to_date_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["MTD Revenue"]
        assert m.type == MetricType.CUMULATIVE
        assert m.grain_to_date == GrainToDate.MONTH
        assert m.window is None

    def test_cumulative_max_parsed(self) -> None:
        model = _load_model()
        m = model.metrics["30-Day Peak Revenue"]
        assert m.cumulative_type == CumulativeAggType.MAX
        assert m.window == 30


class TestMetricValidation:
    def test_derived_requires_expression(self) -> None:
        with pytest.raises(ValueError, match="expression"):
            Metric(label="Bad", type=MetricType.DERIVED)

    def test_cumulative_requires_measure(self) -> None:
        with pytest.raises(ValueError, match="measure"):
            Metric(
                label="Bad",
                type=MetricType.CUMULATIVE,
                time_dimension="Order Date",
            )

    def test_cumulative_requires_time_dimension(self) -> None:
        with pytest.raises(ValueError, match="timeDimension"):
            Metric(
                label="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
            )

    def test_cumulative_rejects_expression(self) -> None:
        with pytest.raises(ValueError, match="must not have"):
            Metric(
                label="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
                time_dimension="Order Date",
                expression="{[Revenue]}",
            )

    def test_window_and_grain_to_date_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            Metric(
                label="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
                time_dimension="Order Date",
                window=7,
                grain_to_date=GrainToDate.MONTH,
            )

    def test_window_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="window"):
            Metric(
                label="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
                time_dimension="Order Date",
                window=0,
            )


# ── Resolution tests ──────────────────────────────────────────────────────


class TestCumulativeResolution:
    def test_resolve_cumulative_metric(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Cumulative Revenue"],
            ),
        )
        resolved = resolver.resolve(query, model)
        assert len(resolved.measures) == 2
        cum = resolved.measures[1]
        assert cum.name == "Cumulative Revenue"
        assert cum.is_cumulative
        assert cum.cumulative_measure == "Revenue"
        assert cum.cumulative_time_dimension == "Order Date"
        assert cum.cumulative_type == CumulativeAggType.SUM
        assert resolved.has_cumulative

    def test_cumulative_unknown_measure_error(self) -> None:
        """Unknown measure reference is caught at parse time by the resolver."""
        yaml = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: DB
    schema: S
    columns:
      D:
        code: D
        abstractType: date
dimensions:
  Dim:
    dataObject: T
    column: D
    resultType: date
metrics:
  Bad:
    type: cumulative
    measure: NonExistent
    timeDimension: Dim
"""
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(yaml)
        _model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        assert any("NonExistent" in e.message for e in result.errors)

    def test_cumulative_unknown_time_dimension_error(self) -> None:
        """Unknown timeDimension should be caught at parse time (not resolution)."""
        yaml_content = """\
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
measures:
  M:
    columns:
      - dataObject: T
        column: V
    aggregation: sum
metrics:
  Bad:
    type: cumulative
    measure: M
    timeDimension: NonExistent
"""
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(yaml_content)
        _model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        assert any("NonExistent" in e.message for e in result.errors)
        assert any(e.code == "CUMULATIVE_UNKNOWN_TIME_DIMENSION" for e in result.errors)

    def test_cumulative_time_dim_not_in_select_error(self) -> None:
        """timeDimension must be in the query's selected dimensions."""
        model = _load_model()
        resolver = QueryResolver()
        # Select Cumulative Revenue but NOT Order Date
        query = QueryObject(
            select=QuerySelect(dimensions=[], measures=["Cumulative Revenue"]),
        )
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(query, model)
        assert any(
            "CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT" in e.code for e in exc_info.value.errors
        )


# ── Wrapper CTE tests ─────────────────────────────────────────────────────


def _make_dim(name: str = "Order Date", object_name: str = "Orders") -> ResolvedDimension:
    return ResolvedDimension(
        name=name,
        object_name=object_name,
        column_name=name,
        source_column="ORDER_DATE",
    )


def _make_measure(
    name: str = "Revenue",
    aggregation: str = "sum",
) -> ResolvedMeasure:
    return ResolvedMeasure(
        name=name,
        aggregation=aggregation,
        expression=FunctionCall(
            name=aggregation.upper(),
            args=[ColumnRef(name="AMOUNT", table="Orders")],
        ),
    )


def _make_cumulative(
    name: str = "Cumulative Revenue",
    measure: str = "Revenue",
    time_dim: str = "Order Date",
    cum_type: CumulativeAggType = CumulativeAggType.SUM,
    window: int | None = None,
    grain_to_date: GrainToDate | None = None,
) -> ResolvedMeasure:
    return ResolvedMeasure(
        name=name,
        aggregation="sum",
        expression=ColumnRef(name=measure),
        is_expression=True,
        component_measures=[measure],
        is_cumulative=True,
        cumulative_measure=measure,
        cumulative_time_dimension=time_dim,
        cumulative_type=cum_type,
        cumulative_window=window,
        cumulative_grain_to_date=grain_to_date,
    )


def _make_ast(
    dim_name: str = "Order Date",
    measure_names: list[str] | None = None,
    order_by: list[OrderByItem] | None = None,
    limit: int | None = None,
) -> Select:
    if measure_names is None:
        measure_names = ["Revenue"]
    columns: list[AliasedExpr] = [
        AliasedExpr(expr=ColumnRef(name="ORDER_DATE", table="Orders"), alias=dim_name),
    ]
    for mname in measure_names:
        columns.append(
            AliasedExpr(
                expr=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
                alias=mname,
            )
        )
    return Select(
        columns=columns,
        from_=From(source="WAREHOUSE.PUBLIC.ORDERS", alias="Orders"),
        group_by=[ColumnRef(name="ORDER_DATE", table="Orders")],
        order_by=order_by or [],
        limit=limit,
    )


class TestNoCumulative:
    def test_returns_ast_unchanged(self) -> None:
        ast = _make_ast()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure()],
            base_object="Orders",
        )
        result = wrap_with_cumulative(ast, resolved)
        assert result is ast


class TestRunningTotal:
    def test_wraps_with_cte(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Cumulative Revenue"])
        revenue = _make_measure()
        cum = _make_cumulative()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        assert len(result.ctes) == 1
        assert result.ctes[0].name == "cumulative_base"
        assert result.from_ is not None
        assert result.from_.source == "cumulative_base"
        assert result.group_by == []

    def test_running_total_window_function(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Cumulative Revenue"])
        revenue = _make_measure()
        cum = _make_cumulative()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        # 3 columns: dim + Revenue + Cumulative Revenue
        assert len(result.columns) == 3
        cum_col = result.columns[2]
        assert isinstance(cum_col, AliasedExpr)
        assert cum_col.alias == "Cumulative Revenue"
        assert isinstance(cum_col.expr, WindowFunction)
        assert cum_col.expr.func_name == "SUM"
        assert cum_col.expr.frame is not None
        assert cum_col.expr.frame.start == "UNBOUNDED PRECEDING"
        assert cum_col.expr.frame.end == "CURRENT ROW"
        assert cum_col.expr.partition_by == []
        assert len(cum_col.expr.order_by) == 1

    def test_regular_measure_passthrough(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Cumulative Revenue"])
        revenue = _make_measure()
        cum = _make_cumulative()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        regular = result.columns[1]
        assert isinstance(regular, AliasedExpr)
        assert regular.alias == "Revenue"
        assert isinstance(regular.expr, ColumnRef)


class TestRollingWindow:
    def test_rolling_7_period(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Rolling Avg"])
        revenue = _make_measure()
        cum = _make_cumulative(
            name="Rolling Avg",
            cum_type=CumulativeAggType.AVG,
            window=7,
        )
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        cum_col = result.columns[2]
        assert isinstance(cum_col, AliasedExpr)
        assert isinstance(cum_col.expr, WindowFunction)
        assert cum_col.expr.func_name == "AVG"
        assert cum_col.expr.frame is not None
        assert cum_col.expr.frame.start == "6 PRECEDING"
        assert cum_col.expr.frame.end == "CURRENT ROW"
        assert cum_col.expr.partition_by == []

    def test_rolling_window_1(self) -> None:
        """window=1 means current row only (0 PRECEDING)."""
        ast = _make_ast(measure_names=["Revenue", "Current"])
        revenue = _make_measure()
        cum = _make_cumulative(name="Current", window=1)
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        cum_col = result.columns[2]
        assert isinstance(cum_col, AliasedExpr)
        assert isinstance(cum_col.expr, WindowFunction)
        assert cum_col.expr.frame is not None
        assert cum_col.expr.frame.start == "0 PRECEDING"


class TestGrainToDate:
    def test_mtd_partitions_by_month(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "MTD Revenue"])
        revenue = _make_measure()
        cum = _make_cumulative(
            name="MTD Revenue",
            grain_to_date=GrainToDate.MONTH,
        )
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        cum_col = result.columns[2]
        assert isinstance(cum_col, AliasedExpr)
        assert isinstance(cum_col.expr, WindowFunction)
        assert cum_col.expr.func_name == "SUM"
        assert len(cum_col.expr.partition_by) == 1
        part = cum_col.expr.partition_by[0]
        assert isinstance(part, FunctionCall)
        assert part.name == "DATE_TRUNC"
        assert cum_col.expr.frame is not None
        assert cum_col.expr.frame.start == "UNBOUNDED PRECEDING"

    def test_ytd_partitions_by_year(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "YTD Revenue"])
        revenue = _make_measure()
        cum = _make_cumulative(
            name="YTD Revenue",
            grain_to_date=GrainToDate.YEAR,
        )
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        cum_col = result.columns[2]
        assert isinstance(cum_col, AliasedExpr)
        assert isinstance(cum_col.expr, WindowFunction)
        part = cum_col.expr.partition_by[0]
        assert isinstance(part, FunctionCall)
        # DATE_TRUNC('year', time_dim)
        assert len(part.args) == 2
        assert isinstance(part.args[0], Literal)
        assert part.args[0].value == "year"


class TestCumulativeAggTypes:
    @pytest.mark.parametrize(
        "agg_type,expected_func",
        [
            (CumulativeAggType.SUM, "SUM"),
            (CumulativeAggType.AVG, "AVG"),
            (CumulativeAggType.MIN, "MIN"),
            (CumulativeAggType.MAX, "MAX"),
            (CumulativeAggType.COUNT, "COUNT"),
        ],
    )
    def test_agg_type_maps_to_function(
        self, agg_type: CumulativeAggType, expected_func: str
    ) -> None:
        ast = _make_ast(measure_names=["Revenue", "Cum"])
        revenue = _make_measure()
        cum = _make_cumulative(name="Cum", cum_type=agg_type)
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        cum_col = result.columns[2]
        assert isinstance(cum_col, AliasedExpr)
        assert isinstance(cum_col.expr, WindowFunction)
        assert cum_col.expr.func_name == expected_func


class TestOrderByAndLimit:
    def test_order_by_remapped(self) -> None:
        ast = _make_ast(
            measure_names=["Revenue", "Cumulative Revenue"],
            order_by=[OrderByItem(expr=ColumnRef(name="ORDER_DATE", table="Orders"), desc=False)],
        )
        revenue = _make_measure()
        cum = _make_cumulative()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
            order_by_exprs=[
                (ColumnRef(name="ORDER_DATE", table="Orders"), False),
            ],
        )
        result = wrap_with_cumulative(ast, resolved)
        assert len(result.order_by) == 1
        assert isinstance(result.order_by[0].expr, ColumnRef)
        assert result.order_by[0].expr.table is None
        # Should use dimension alias, not physical column code
        assert result.order_by[0].expr.name == "Order Date"

    def test_limit_on_outer(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Cumulative Revenue"], limit=10)
        revenue = _make_measure()
        cum = _make_cumulative()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        assert result.limit == 10
        base_cte = result.ctes[-1]
        assert isinstance(base_cte.query, Select)
        assert base_cte.query.limit is None


# ── End-to-end SQL generation tests ───────────────────────────────────────


class TestCumulativeSQLGeneration:
    def test_running_total_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Cumulative Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "OVER" in sql
        assert "UNBOUNDED PRECEDING" in sql
        assert "CURRENT ROW" in sql
        # sql_valid may be False if sqlglot warns on CTEs — that's ok

    def test_rolling_window_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "7-Day Rolling Avg Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "AVG" in sql
        assert "6 PRECEDING" in sql

    def test_grain_to_date_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "MTD Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "PARTITION BY" in sql
        assert "DATE_TRUNC" in sql

    def test_multiple_cumulative_metrics(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=[
                    "Revenue",
                    "Cumulative Revenue",
                    "MTD Revenue",
                    "YTD Revenue",
                ],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        # Should have multiple window functions
        assert sql.count("OVER") >= 3

    def test_explain_has_cumulative(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue", "Cumulative Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        assert result.explain is not None
        assert result.explain.has_cumulative

    def test_derived_metric_alongside_cumulative(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Date"],
                measures=["Revenue per Order", "Cumulative Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql.upper()
        assert "OVER" in sql
