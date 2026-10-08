"""Tests for cumulative metrics: model, resolution, wrapping, and SQL generation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
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
from orionbelt.models.query import QueryFilter, QueryFilterGroup, QueryObject, QuerySelect
from orionbelt.models.semantic import (
    CumulativeAggType,
    GrainToDate,
    Metric,
    MetricType,
    SemanticModel,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

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
      Region:
        code: REGION
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
      Booked Date:
        abstractType: date
        expression: "{Order Date}"
      Region Code:
        abstractType: string
        expression: "upper({Region})"

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month

  Order Year:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: year

  Region:
    dataObject: Orders
    column: Region
    resultType: string

  Booked Month:
    dataObject: Orders
    column: Booked Date
    resultType: date
    timeGrain: month

  Region Code:
    dataObject: Orders
    column: Region Code
    resultType: string

  Order Amount:
    dataObject: Orders
    column: Amount
    resultType: float

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

  # Cumulative over a computed date column
  Cumulative Booked Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Booked Month

  # Window metric partitioned by a float dimension
  Revenue Rank by Amount:
    type: window
    measure: Revenue
    windowFunction: rank
    partitionBy: [Order Amount]

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
            Metric(name="Bad", type=MetricType.DERIVED)

    def test_cumulative_requires_measure(self) -> None:
        with pytest.raises(ValueError, match="measure"):
            Metric(
                name="Bad",
                type=MetricType.CUMULATIVE,
                time_dimension="Order Date",
            )

    def test_cumulative_requires_time_dimension(self) -> None:
        with pytest.raises(ValueError, match="timeDimension"):
            Metric(
                name="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
            )

    def test_cumulative_rejects_expression(self) -> None:
        with pytest.raises(ValueError, match="must not have"):
            Metric(
                name="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
                time_dimension="Order Date",
                expression="{[Revenue]}",
            )

    def test_window_and_grain_to_date_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            Metric(
                name="Bad",
                type=MetricType.CUMULATIVE,
                measure="Revenue",
                time_dimension="Order Date",
                window=7,
                grain_to_date=GrainToDate.MONTH,
            )

    def test_window_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="window"):
            Metric(
                name="Bad",
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

    def test_cumulative_time_dim_may_be_left_out(self) -> None:
        """Without its timeDimension selected, the metric is evaluated as of one period."""
        query = QueryObject(select=QuerySelect(dimensions=[], measures=["Cumulative Revenue"]))
        resolved = QueryResolver().resolve(query, _load_model())
        assert [m.name for m in resolved.measures] == ["Cumulative Revenue"]
        assert not resolved.warnings

    def test_cumulative_time_dim_not_in_select_under_grouping_error(self) -> None:
        """A subtotal row has no single group to evaluate the metric as of a period for."""
        query = QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Cumulative Revenue"]),
            grouping="rollup",
        )
        with pytest.raises(ResolutionError) as exc_info:
            QueryResolver().resolve(query, _load_model())
        assert any(
            "CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT" in e.code for e in exc_info.value.errors
        )

    def test_as_of_with_the_time_dim_selected_warns(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Cumulative Revenue"]),
            asOf="2021-04-15",
        )
        resolved = QueryResolver().resolve(query, _load_model())
        assert [w.code for w in resolved.warnings] == ["CUMULATIVE_CONSTRAINT_VIOLATED"]


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
    """A rolling window joins the periods its window reaches, not the rows."""

    @staticmethod
    def _rolling(window: int, cum_type: CumulativeAggType) -> Select:
        ast = _make_ast(measure_names=["Revenue", "Rolling"])
        revenue = _make_measure()
        cum = _make_cumulative(name="Rolling", cum_type=cum_type, window=window)
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[revenue, cum],
            base_object="Orders",
            metric_components={"Revenue": revenue},
        )
        result = wrap_with_cumulative(ast, resolved)
        rolling = next(c for c in result.ctes if c.name == "cumulative_rolling")
        assert isinstance(rolling.query, Select)
        return rolling.query

    @staticmethod
    def _reach(query: Select) -> Literal:
        """The upper bound of the join's ``date_diff(...) <= N - 1``.

        The join is ``(0 <= date_diff <= N - 1) OR (both dates NULL)``.
        """
        on = query.joins[0].on
        assert isinstance(on, BinaryOp) and on.op == "OR"
        within = on.left
        assert isinstance(within, BinaryOp) and within.op == "AND"
        upper = within.right
        assert isinstance(upper, BinaryOp) and upper.op == "<="
        assert isinstance(upper.left, FunctionCall) and upper.left.name == "date_diff"
        assert isinstance(upper.right, Literal)
        return upper.right

    def test_rolling_7_period(self) -> None:
        query = self._rolling(7, CumulativeAggType.AVG)
        aggregate = query.columns[-1]
        assert isinstance(aggregate, AliasedExpr) and aggregate.alias == "Rolling"
        assert isinstance(aggregate.expr, FunctionCall) and aggregate.expr.name == "AVG"
        assert self._reach(query).value == 6

    def test_rolling_window_1(self) -> None:
        """window=1 reaches the current period only."""
        assert self._reach(self._rolling(1, CumulativeAggType.SUM)).value == 0


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
                (ColumnRef(name="ORDER_DATE", table="Orders"), False, None),
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
        assert "DATE_DIFF('MONTH'" in sql
        assert "PRECEDING" not in sql.split('"CUMULATIVE_ROLLING" AS')[1]

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


# ── Partitioning by the query's other dimensions (executed on DuckDB) ─────


@pytest.fixture
def orders() -> Iterator[Any]:
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA PUBLIC")
    connection.execute(
        "CREATE TABLE PUBLIC.ORDERS AS SELECT * FROM (VALUES "
        "('1', DATE '2021-11-05', 'East', 10.0), "
        "('2', DATE '2021-11-20', 'West', 1.0), "
        "('3', DATE '2021-12-05', 'East', 20.0), "
        "('4', DATE '2021-12-10', 'West', 2.0), "
        "('5', DATE '2022-01-05', 'East', 40.0), "
        "('6', DATE '2022-01-10', 'West', 4.0)"
        ") AS t(ORDER_ID, ORDER_DATE, REGION, AMOUNT)"
    )
    try:
        yield connection
    finally:
        connection.close()


class TestCumulativePartitionsByQueryDimensions:
    """A cumulative metric accumulates per group of the query's other dimensions."""

    @staticmethod
    def _run(connection: Any, dimensions: list[str], metric: str) -> dict[tuple[str, ...], float]:
        query = QueryObject(select=QuerySelect(dimensions=dimensions, measures=[metric]))
        sql = CompilationPipeline().compile(query, _load_model(), "duckdb").sql
        return {
            tuple(str(v) for v in row[:-1]): float(row[-1])
            for row in connection.execute(sql).fetchall()
        }

    def test_running_total_per_region(self, orders: Any) -> None:
        rows = self._run(orders, ["Region", "Order Date"], "Cumulative Revenue")
        assert rows[("East", "2021-11-01")] == 10.0
        assert rows[("West", "2021-11-01")] == 1.0
        assert rows[("East", "2021-12-01")] == 30.0
        assert rows[("West", "2021-12-01")] == 3.0
        assert rows[("West", "2022-01-01")] == 7.0

    def test_grain_to_date_per_region(self, orders: Any) -> None:
        rows = self._run(orders, ["Region", "Order Date"], "YTD Revenue")
        assert rows[("West", "2021-12-01")] == 3.0
        assert rows[("East", "2022-01-01")] == 40.0
        assert rows[("West", "2022-01-01")] == 4.0

    def test_same_column_coarser_grain_does_not_partition(self, orders: Any) -> None:
        # Order Year is a position on the time axis, not a group: the running
        # total continues across the year boundary.
        rows = self._run(orders, ["Order Year", "Order Date"], "Cumulative Revenue")
        assert rows[("2022-01-01", "2022-01-01")] == 77.0

    def test_computed_columns_on_one_object_are_not_one_time_axis(self, orders: Any) -> None:
        # Every computed column has an empty physical name; Region Code must
        # still partition a running total over the computed Booked Month.
        rows = self._run(orders, ["Region Code", "Booked Month"], "Cumulative Booked Revenue")
        assert rows[("WEST", "2021-11-01")] == 1.0
        assert rows[("EAST", "2021-12-01")] == 30.0
        assert rows[("WEST", "2022-01-01")] == 7.0

    def test_partition_by_not_repeated_when_selected(self) -> None:
        model = _load_model(
            CUMULATIVE_MODEL_YAML
            + """
  Revenue by Region:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    partitionBy: [Region]
"""
        )
        query = QueryObject(
            select=QuerySelect(dimensions=["Region", "Order Date"], measures=["Revenue by Region"])
        )
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert 'PARTITION BY "Region" ORDER BY' in sql


@pytest.fixture
def gappy() -> Iterator[Any]:
    """East has no March; West sold only in May."""
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA PUBLIC")
    connection.execute(
        "CREATE TABLE PUBLIC.ORDERS AS SELECT * FROM (VALUES "
        "('1', DATE '2021-01-05', 'East', 1.0), "
        "('2', DATE '2021-02-05', 'East', 10.0), "
        "('3', DATE '2021-04-05', 'East', 1000.0), "
        "('4', DATE '2021-05-05', 'East', 100.0), "
        "('5', DATE '2021-05-07', 'West', 5.0)"
        ") AS t(ORDER_ID, ORDER_DATE, REGION, AMOUNT)"
    )
    try:
        yield connection
    finally:
        connection.close()


def _run_gappy(
    connection: Any, query: QueryObject, model_yaml: str
) -> dict[tuple[str, ...], tuple[float | None, ...]]:
    """Rows keyed by their dimension values, the measures as floats."""
    sql = CompilationPipeline().compile(query, _load_model(model_yaml), "duckdb").sql
    width = len(query.select.dimensions)
    return {
        tuple(str(v) for v in row[:width]): tuple(
            None if v is None else float(v) for v in row[width:]
        )
        for row in connection.execute(sql).fetchall()
    }


class TestRollingWindowCountsCalendarPeriods:
    """``window: N`` reaches back N periods of the time dimension's grain.

    Framed by rows, a window over months with a gap reached past the gap: East
    has no March, so May's three-row window read February. A period without
    data contributes nothing, and ``avg`` averages the periods that have data.
    """

    MODEL_YAML = CUMULATIVE_MODEL_YAML.replace(
        "  # Cumulative: rolling max",
        """  Rolling 3 Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    window: 3

  # Cumulative: rolling max""",
    )

    def _run(
        self, connection: Any, query: QueryObject
    ) -> dict[tuple[str, ...], tuple[float | None, ...]]:
        return _run_gappy(connection, query, self.MODEL_YAML)

    def test_a_gap_counts_as_a_period(self, gappy: Any) -> None:
        rows = self._run(
            gappy,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Region", "Order Date"], measures=["Rolling 3 Revenue"]
                )
            ),
        )
        assert rows == {
            ("East", "2021-01-01"): (1.0,),
            ("East", "2021-02-01"): (11.0,),
            ("East", "2021-04-01"): (1010.0,),  # February to April
            ("East", "2021-05-01"): (1100.0,),  # March to May; rows read 1110
            ("West", "2021-05-01"): (5.0,),
        }

    def test_windows_of_different_widths_in_one_query(self, gappy: Any) -> None:
        rows = self._run(
            gappy,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Region", "Order Date"],
                    measures=["Rolling 3 Revenue", "7-Day Rolling Avg Revenue"],
                )
            ),
        )
        # Seven months back from May reach January: the four East months with
        # data average 277.75.
        assert rows[("East", "2021-05-01")] == (1100.0, 277.75)
        assert rows[("East", "2021-04-01")] == (1010.0, 337.0)

    def test_a_time_filter_does_not_cut_the_window(self, gappy: Any) -> None:
        rows = self._run(
            gappy,
            QueryObject(
                select=QuerySelect(dimensions=["Order Date"], measures=["Rolling 3 Revenue"]),
                where=[QueryFilter(field="Order Date", op="gte", value="2021-05-01")],
            ),
        )
        assert rows == {("2021-05-01",): (1105.0,)}

    def test_a_row_without_a_date_keeps_its_own_value(self, gappy: Any) -> None:
        """A NULL date has no periods before it; the join used to drop it."""
        gappy.execute("INSERT INTO PUBLIC.ORDERS VALUES ('6', NULL, 'East', 20.0)")
        rows = self._run(
            gappy,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Region", "Order Date"],
                    measures=["Rolling 3 Revenue", "7-Day Rolling Avg Revenue"],
                )
            ),
        )
        assert rows[("East", "None")] == (20.0, 20.0)
        assert rows[("East", "2021-05-01")] == (1100.0, 277.75)

    def test_running_totals_keep_their_window_function(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Cumulative Revenue"])
        )
        sql = CompilationPipeline().compile(query, _load_model(self.MODEL_YAML), "duckdb").sql
        assert "cumulative_rolling" not in sql
        assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in sql


class TestAsOf:
    """Without its time dimension selected, a cumulative metric is evaluated as of one period.

    Each group gets the value its row for that period shows with the time
    dimension selected, also when the group has no data in the period itself.
    """

    MODEL_YAML = TestRollingWindowCountsCalendarPeriods.MODEL_YAML
    METRICS = ["Cumulative Revenue", "Rolling 3 Revenue", "YTD Revenue", "MTD Revenue"]

    def _run(
        self,
        connection: Any,
        dimensions: list[str],
        as_of: str | None = None,
        where: list[QueryFilter] | None = None,
    ) -> dict[tuple[str, ...], tuple[float | None, ...]]:
        query = QueryObject(
            select=QuerySelect(dimensions=dimensions, measures=self.METRICS),
            where=where or [],
            asOf=as_of,
        )
        return _run_gappy(connection, query, self.MODEL_YAML)

    def test_the_latest_period_with_data_by_default(self, gappy: Any) -> None:
        assert self._run(gappy, ["Region"]) == {
            ("East",): (1111.0, 1100.0, 1111.0, 100.0),
            ("West",): (5.0, 5.0, 5.0, 5.0),
        }

    def test_equals_the_row_for_the_period_with_the_time_dimension_selected(
        self, gappy: Any
    ) -> None:
        by_month = _run_gappy(
            gappy,
            QueryObject(
                select=QuerySelect(dimensions=["Region", "Order Date"], measures=self.METRICS)
            ),
            self.MODEL_YAML,
        )
        assert self._run(gappy, ["Region"], as_of="2021-04-15") == {
            ("East",): by_month[("East", "2021-04-01")],
            ("West",): (None, None, None, None),  # nothing until May
        }

    def test_a_period_without_data_reads_the_periods_before_it(self, gappy: Any) -> None:
        """No March anywhere: the running total and the window read January and
        February; month-to-date has no March to read."""
        assert self._run(gappy, ["Region"], as_of="2021-03-10")[("East",)] == (
            11.0,
            11.0,
            11.0,
            None,
        )

    def test_without_dimensions_one_row(self, gappy: Any) -> None:
        assert self._run(gappy, []) == {(): (1116.0, 1105.0, 1116.0, 105.0)}

    def test_a_time_filter_picks_the_period(self, gappy: Any) -> None:
        before_may = [QueryFilter(field="Order Date", op="lt", value="2021-05-01")]
        assert self._run(gappy, ["Region"], where=before_may) == {
            ("East",): (1011.0, 1010.0, 1011.0, 1000.0),
        }

    def test_an_as_of_date_is_read_at_the_time_dimension_grain(self, gappy: Any) -> None:
        """2021-05-01 is a Saturday: its week starts on April 26, so it is
        April's week, and month-to-date over weeks reads April."""
        model_yaml = self.MODEL_YAML.replace(
            "  Region:\n    dataObject: Orders",
            """  Order Week:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: week

  Region:
    dataObject: Orders""",
        ).replace(
            "  # Cumulative: rolling max",
            """  Weekly MTD Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Week
    grainToDate: month

  # Cumulative: rolling max""",
        )
        query = QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Weekly MTD Revenue"]),
            asOf="2021-05-01",
        )
        assert _run_gappy(gappy, query, model_yaml)[("East",)] == (1000.0,)

    def test_a_count_over_an_empty_range_is_null(self, gappy: Any) -> None:
        """No March: a one-month count as of March has nothing to count, while
        the running count reads January and February."""
        model_yaml = self.MODEL_YAML.replace(
            "  # Cumulative: rolling max",
            """  Monthly Order Count:
    type: cumulative
    measure: Order Count
    timeDimension: Order Date
    cumulativeType: count
    window: 1

  Running Order Count:
    type: cumulative
    measure: Order Count
    timeDimension: Order Date
    cumulativeType: count

  # Cumulative: rolling max""",
        )
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region"], measures=["Monthly Order Count", "Running Order Count"]
            ),
            asOf="2021-03-10",
        )
        assert _run_gappy(gappy, query, model_yaml)[("East",)] == (None, 2.0)
        before_any = QueryObject(
            select=QuerySelect(measures=["Monthly Order Count", "Running Order Count"]),
            asOf="2020-12-01",
        )
        assert _run_gappy(gappy, before_any, model_yaml) == {(): (None, None)}

    def test_a_date_is_not_truncated_to_a_finer_grain(self) -> None:
        """A date already starts its hour; BigQuery truncates only a
        timestamp to one."""
        model_yaml = self.MODEL_YAML.replace(
            "  Region:\n    dataObject: Orders",
            """  Order Hour:
    dataObject: Orders
    column: Order Date
    resultType: timestamp
    timeGrain: hour

  Region:
    dataObject: Orders""",
        ).replace(
            "  # Cumulative: rolling max",
            """  Hourly Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Hour

  # Cumulative: rolling max""",
        )
        query = QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Hourly Running Revenue"]),
            asOf="2025-02-01",
        )
        sql = CompilationPipeline().compile(query, _load_model(model_yaml), "bigquery").sql
        assert "CAST('2025-02-01' AS DATE)" in sql
        assert "DATE_TRUNC(CAST('2025-02-01'" not in sql

    def test_the_tables_the_periods_read_key_the_cache(self) -> None:
        """The time dimension comes from a calendar the shown rows do not join."""
        model_yaml = (
            self.MODEL_YAML.replace(
                """      Region Code:
        abstractType: string
        expression: "upper({Region})"
""",
                """      Region Code:
        abstractType: string
        expression: "upper({Region})"
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom:
          - Order Date
        columnsTo:
          - Day

  Calendar:
    code: CALENDAR
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Day:
        code: DAY
        abstractType: date
""",
            )
            .replace(
                "  Region:\n    dataObject: Orders",
                """  Calendar Month:
    dataObject: Calendar
    column: Day
    resultType: date
    timeGrain: month

  Region:
    dataObject: Orders""",
            )
            .replace(
                "  # Cumulative: rolling max",
                """  Calendar YTD Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Calendar Month
    grainToDate: year

  # Cumulative: rolling max""",
            )
        )
        query = QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Calendar YTD Revenue"])
        )
        result = CompilationPipeline().compile(query, _load_model(model_yaml), "duckdb")
        assert '"CALENDAR"' in result.sql
        assert result.physical_tables == ["WAREHOUSE.PUBLIC.CALENDAR", "WAREHOUSE.PUBLIC.ORDERS"]

    def test_another_grain_of_the_same_date_is_refused(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Year"], measures=["Cumulative Revenue"])
        )
        with pytest.raises(ResolutionError) as exc_info:
            CompilationPipeline().compile(query, _load_model(self.MODEL_YAML), "duckdb")
        assert [e.code for e in exc_info.value.errors] == [
            "CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT"
        ]


class TestTimeFilterDoesNotCutTheLookBack:
    """A time filter picks the periods shown; the cumulative values read past it.

    Applied to the source rows, the filter cut the history away: year-to-date
    started at the first month shown, and a running total at the filter's
    start. The plain measures still respect the filter.
    """

    #: A measure that ignores the query's filters, and a period-over-period
    #: metric: each puts CTEs of its own under the cumulative wrapper.
    EXTENDED_MODEL_YAML = CUMULATIVE_MODEL_YAML.replace(
        "\nmetrics:\n",
        """
  Unfiltered Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    filterContext:
      mode: FIXED

metrics:
  Order Count MoM:
    type: period_over_period
    expression: '{[Order Count]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
""",
        1,
    )

    @staticmethod
    def _run(
        connection: Any,
        dimensions: list[str],
        measures: list[str],
        where: list[QueryFilter | QueryFilterGroup],
        model_yaml: str = CUMULATIVE_MODEL_YAML,
    ) -> dict[tuple[str, ...], tuple[float | None, ...]]:
        query = QueryObject(
            select=QuerySelect(dimensions=dimensions, measures=measures), where=where
        )
        sql = CompilationPipeline().compile(query, _load_model(model_yaml), "duckdb").sql
        width = len(dimensions)
        return {
            tuple(str(v) for v in row[:width]): tuple(
                None if v is None else float(v) for v in row[width:]
            )
            for row in connection.execute(sql).fetchall()
        }

    def test_year_to_date_reads_the_months_before_the_filter(self, orders: Any) -> None:
        rows = self._run(
            orders,
            ["Region", "Order Date"],
            ["Revenue", "YTD Revenue"],
            [QueryFilter(field="Order Date", op="gte", value="2021-12-01")],
        )
        assert set(rows) == {
            ("East", "2021-12-01"),
            ("West", "2021-12-01"),
            ("East", "2022-01-01"),
            ("West", "2022-01-01"),
        }
        assert rows[("East", "2021-12-01")] == (20.0, 30.0)
        assert rows[("West", "2021-12-01")] == (2.0, 3.0)
        assert rows[("East", "2022-01-01")] == (40.0, 40.0)

    def test_a_coarser_grain_of_the_same_column_is_a_time_filter(self, orders: Any) -> None:
        rows = self._run(
            orders,
            ["Region", "Order Date"],
            ["Cumulative Revenue"],
            [QueryFilter(field="Order Year", op="gte", value="2022-01-01")],
        )
        assert rows == {
            ("East", "2022-01-01"): (70.0,),
            ("West", "2022-01-01"): (7.0,),
        }

    def test_a_filter_inside_a_period_still_filters_the_plain_measure(self, orders: Any) -> None:
        # East's December order is on the 5th: the row disappears, and West's
        # December revenue counts the 10th only, while its year-to-date reads
        # the whole history.
        rows = self._run(
            orders,
            ["Region", "Order Date"],
            ["Revenue", "YTD Revenue"],
            [QueryFilter(field="Order Date", op="gte", value="2021-12-08")],
        )
        assert ("East", "2021-12-01") not in rows
        assert rows[("West", "2021-12-01")] == (2.0, 3.0)
        assert rows[("West", "2022-01-01")] == (4.0, 4.0)

    def test_a_filter_on_another_dimension_still_limits_the_history(self, orders: Any) -> None:
        rows = self._run(
            orders,
            ["Order Date"],
            ["Cumulative Revenue"],
            [
                QueryFilter(field="Region", op="equals", value="East"),
                QueryFilter(field="Order Date", op="gte", value="2022-01-01"),
            ],
        )
        assert rows == {("2022-01-01",): (70.0,)}

    def test_a_date_range_written_as_a_group_is_a_time_filter(self, orders: Any) -> None:
        rows = self._run(
            orders,
            ["Order Date"],
            ["Cumulative Revenue"],
            [
                QueryFilterGroup(
                    logic="and",
                    filters=[
                        QueryFilter(field="Order Date", op="gte", value="2021-12-01"),
                        QueryFilter(field="Order Date", op="lt", value="2022-02-01"),
                    ],
                )
            ],
        )
        assert rows == {("2021-12-01",): (33.0,), ("2022-01-01",): (77.0,)}

    def test_a_static_filter_survives_a_group_repeating_it(self, orders: Any) -> None:
        # The group's lower bound is also the model's static filter. Taking the
        # group out conjunct by conjunct took the static filter with it, and
        # November entered December's running total.
        static = (
            "\nfilters:\n"
            "  - dataObject: Orders\n"
            "    column: Order Date\n"
            '    operator: ">="\n'
            "    value: 2021-12-01\n"
        )
        rows = self._run(
            orders,
            ["Order Date"],
            ["Cumulative Revenue"],
            [
                QueryFilterGroup(
                    logic="and",
                    filters=[
                        QueryFilter(field="Order Date", op="gte", value="2021-12-01"),
                        QueryFilter(field="Order Date", op="lt", value="2022-01-01"),
                    ],
                )
            ],
            CUMULATIVE_MODEL_YAML + static,
        )
        assert rows == {("2021-12-01",): (22.0,)}

    def test_beside_a_measure_with_its_own_filter_context(self, orders: Any) -> None:
        # The filterContext measure puts the query in CTEs of its own, so the
        # look-back reads the base measure by alias. It re-derived it over the
        # fact table instead, which that CTE does not join.
        rows = self._run(
            orders,
            ["Order Date"],
            ["Unfiltered Revenue", "YTD Revenue"],
            [QueryFilter(field="Order Date", op="gte", value="2021-12-01")],
            self.EXTENDED_MODEL_YAML,
        )
        assert rows == {("2021-12-01",): (22.0, 33.0), ("2022-01-01",): (44.0, 44.0)}

    def test_beside_a_period_over_period_metric(self, orders: Any) -> None:
        # The comparison's CTEs are SQL text with no WHERE to take the filter
        # out of; the running total reads the comparison's own look-back.
        rows = self._run(
            orders,
            ["Order Date"],
            ["Cumulative Revenue", "Order Count MoM"],
            [QueryFilter(field="Order Date", op="gte", value="2021-12-01")],
            self.EXTENDED_MODEL_YAML,
        )
        assert rows == {("2021-12-01",): (33.0, 0.0), ("2022-01-01",): (77.0, 0.0)}

    def test_no_time_filter_keeps_the_single_window(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Cumulative Revenue"]),
            where=[QueryFilter(field="Region", op="equals", value="East")],
        )
        sql = CompilationPipeline().compile(query, _load_model(), "duckdb").sql
        assert "cumulative_lookback" not in sql
        assert "cumulative_window" not in sql


class TestFloatPartitionKeys:
    """BigQuery refuses a FLOAT64 window partition key; it partitions by its text."""

    @staticmethod
    def _sql(dimensions: list[str], metric: str, dialect: str) -> str:
        query = QueryObject(select=QuerySelect(dimensions=dimensions, measures=[metric]))
        return CompilationPipeline().compile(query, _load_model(), dialect).sql

    def test_cumulative_float_group_cast_on_bigquery(self) -> None:
        sql = self._sql(["Order Amount", "Order Date"], "Cumulative Revenue", "bigquery")
        assert "PARTITION BY CAST(`Order Amount` AS STRING)" in sql

    def test_window_partition_by_float_cast_on_bigquery(self) -> None:
        sql = self._sql(["Order Amount", "Region"], "Revenue Rank by Amount", "bigquery")
        assert "PARTITION BY CAST(`Order Amount` AS STRING)" in sql

    def test_float_key_left_as_is_elsewhere(self) -> None:
        sql = self._sql(["Order Amount", "Order Date"], "Cumulative Revenue", "duckdb")
        assert 'PARTITION BY "Order Amount" ORDER BY' in sql

    def test_non_float_key_not_cast_on_bigquery(self) -> None:
        sql = self._sql(["Region", "Order Date"], "Cumulative Revenue", "bigquery")
        assert "PARTITION BY `Region` ORDER BY" in sql


class TestNonAdditiveCumulativeSum:
    """A cumulative ``sum`` adds up values per period, which only a measure that
    adds up across periods turns into its value over the range."""

    MODEL_YAML = CUMULATIVE_MODEL_YAML.replace(
        """      Booked Date:""",
        """      Stock Level:
        code: STOCK_LEVEL
        abstractType: int
        numClass: non-additive
      Booked Date:""",
    ).replace(
        "metrics:\n",
        """  Customers:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count_distinct

  Distinct Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    distinct: true

  Average Order:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: avg

  Stock:
    columns:
      - dataObject: Orders
        column: Stock Level
    resultType: int
    aggregation: sum

metrics:
""",
        1,
    )

    def _warnings(self, measure: str, cumulative_type: str = "sum") -> list[str]:
        model_yaml = self.MODEL_YAML.replace(
            "  # Cumulative: rolling max",
            f"""  Accumulated:
    type: cumulative
    measure: {measure}
    timeDimension: Order Date
    cumulativeType: {cumulative_type}

  # Cumulative: rolling max""",
        )
        return [
            e.message
            for e in SemanticValidator().validate(_load_model(model_yaml))
            if e.code == "NON_ADDITIVE_CUMULATIVE_SUM" and e.severity == "warning"
        ]

    @pytest.mark.parametrize(
        ("measure", "reason"),
        [
            ("Customers", "aggregates with 'count_distinct'"),
            ("Average Order", "aggregates with 'avg'"),
            ("Distinct Revenue", "aggregates distinct values"),
        ],
    )
    def test_a_sum_over_a_measure_that_does_not_add_up_warns(
        self, measure: str, reason: str
    ) -> None:
        assert self._warnings(measure) == [
            f"Cumulative metric 'Accumulated' sums the values per period of measure "
            f"'{measure}', which {reason}: the sum is not '{measure}' over the whole range."
        ]

    @pytest.mark.parametrize("measure", ["Revenue", "Order Count", "Orders Count", "Stock"])
    def test_a_sum_over_sums_and_counts_does_not_warn(self, measure: str) -> None:
        assert self._warnings(measure) == []

    @pytest.mark.parametrize("cumulative_type", ["avg", "min", "max", "count"])
    def test_other_cumulative_types_do_not_warn(self, cumulative_type: str) -> None:
        assert self._warnings("Customers", cumulative_type) == []
