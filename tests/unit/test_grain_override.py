"""Tests for grain override (Phase 1) — OBML grain property on measures."""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    From,
    FunctionCall,
    Select,
    WindowFunction,
)
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import (
    QueryResolver,
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    _resolve_effective_grain,
)
from orionbelt.compiler.total_wrap import wrap_with_totals
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import GrainMode, GrainOverride, Measure, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# -- Helper YAML models -------------------------------------------------------

GRAIN_MODEL_YAML = """\
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
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
      Region:
        code: REGION
        abstractType: string
      Product:
        code: PRODUCT
        abstractType: string
      Category:
        code: CATEGORY
        abstractType: string

dimensions:
  Region:
    dataObject: Orders
    column: Region
    resultType: string
  Product:
    dataObject: Orders
    column: Product
    resultType: string
  Category:
    dataObject: Orders
    column: Category
    resultType: string

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum

  Region Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED
      include: [Region]

  Grand Total:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED

  Revenue excl Category:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: RELATIVE
      exclude: [Category]

  Adaptive Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED
      keepOnly: [Region, Category]

  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    aggregation: count
    grain:
      mode: FIXED
      include: [Region]

  Avg Revenue by Region:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: avg
    grain:
      mode: FIXED
      include: [Region]

  Min Revenue by Region:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: min
    grain:
      mode: FIXED
      include: [Region]

  Max Revenue by Region:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: max
    grain:
      mode: FIXED
      include: [Region]

metrics:
  Revenue Share:
    expression: '{[Revenue]} / {[Region Revenue]}'
"""


def _load_model(yaml_content: str = GRAIN_MODEL_YAML) -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(yaml_content)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Model errors: {[e.message for e in result.errors]}"
    return model


# -- Pydantic model tests ------------------------------------------------------


class TestGrainOverrideModel:
    def test_fixed_mode_no_exclude(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED, include=["Region"])
        assert grain.mode == GrainMode.FIXED
        assert grain.include == ["Region"]

    def test_fixed_mode_with_exclude_raises(self) -> None:
        with pytest.raises(ValueError, match="FIXED cannot have 'exclude'"):
            GrainOverride(mode=GrainMode.FIXED, exclude=["Region"])

    def test_relative_mode_with_exclude(self) -> None:
        grain = GrainOverride(mode=GrainMode.RELATIVE, exclude=["Category"])
        assert grain.mode == GrainMode.RELATIVE
        assert grain.exclude == ["Category"]

    def test_fixed_empty_is_grand_total(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED)
        assert grain.include == []
        assert grain.keep_only == []

    def test_keep_only_alias(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED, keep_only=["Region", "Product"])
        assert grain.keep_only == ["Region", "Product"]

    def test_total_and_grain_mutual_exclusion(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            Measure(
                name="Bad",
                aggregation="sum",
                total=True,
                grain=GrainOverride(mode=GrainMode.FIXED),
            )


# -- Effective grain resolution ------------------------------------------------


class TestEffectiveGrainResolution:
    def test_fixed_with_include(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED, include=["Region"])
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == ["Region"]

    def test_fixed_empty_grand_total(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED)
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == []

    def test_fixed_keep_only_intersection(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED, keep_only=["Region", "Category"])
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == ["Region"]

    def test_fixed_keep_only_no_overlap(self) -> None:
        grain = GrainOverride(mode=GrainMode.FIXED, keep_only=["Category"])
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == []

    def test_relative_exclude(self) -> None:
        grain = GrainOverride(mode=GrainMode.RELATIVE, exclude=["Product"])
        result = _resolve_effective_grain(grain, ["Region", "Product", "Category"])
        assert result == ["Region", "Category"]

    def test_relative_no_changes(self) -> None:
        grain = GrainOverride(mode=GrainMode.RELATIVE)
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == ["Region", "Product"]

    def test_relative_include_adds(self) -> None:
        grain = GrainOverride(mode=GrainMode.RELATIVE, include=["Category"])
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == ["Region", "Product", "Category"]

    def test_relative_exclude_and_include(self) -> None:
        grain = GrainOverride(mode=GrainMode.RELATIVE, exclude=["Product"], include=["Category"])
        result = _resolve_effective_grain(grain, ["Region", "Product"])
        assert result == ["Region", "Category"]


# -- QueryResolver grain resolution -------------------------------------------


class TestQueryResolverGrain:
    def test_grain_override_propagated(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Region Revenue"],
            ),
        )
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.grain_override is not None
        assert m.grain_override.mode == GrainMode.FIXED
        assert m.effective_grain == ["Region"]

    def test_no_grain_override(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Revenue"]),
        )
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.grain_override is None
        assert m.effective_grain is None

    def test_fixed_empty_effective_grain(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Grand Total"],
            ),
        )
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.effective_grain == []

    def test_relative_exclude_effective_grain(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product", "Category"],
                measures=["Revenue excl Category"],
            ),
        )
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.effective_grain == ["Region", "Product"]

    def test_adaptive_keep_only(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Adaptive Revenue"],
            ),
        )
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.effective_grain == ["Region"]

    def test_grain_superset_rejected(self) -> None:
        yaml = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
      Region:
        code: REGION
        abstractType: string
      Product:
        code: PRODUCT
        abstractType: string
dimensions:
  Region:
    dataObject: Orders
    column: Region
  Product:
    dataObject: Orders
    column: Product
measures:
  Revenue by Product:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED
      include: [Product]
"""
        model = _load_model(yaml)
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region"],
                measures=["Revenue by Product"],
            ),
        )
        with pytest.raises(ResolutionError, match="not a subset of query dimensions"):
            resolver.resolve(query, model)

    def test_has_totals_with_grain_override(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Region Revenue"],
            ),
        )
        resolved = resolver.resolve(query, model)
        assert resolved.has_totals is True

    def test_has_grain_overrides(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Region Revenue"],
            ),
        )
        resolved = resolver.resolve(query, model)
        assert resolved.has_grain_overrides is True

    def test_has_grain_overrides_false(self) -> None:
        model = _load_model()
        resolver = QueryResolver()
        query = QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Revenue"]),
        )
        resolved = resolver.resolve(query, model)
        assert resolved.has_grain_overrides is False


# -- Total wrap with grain override -------------------------------------------


def _make_dim(name: str = "Region") -> ResolvedDimension:
    return ResolvedDimension(
        name=name, object_name="Orders", column_name=name, source_column=name.upper()
    )


def _make_grain_measure(
    name: str = "Region Revenue",
    aggregation: str = "sum",
    effective_grain: list[str] | None = None,
    grain_override: GrainOverride | None = None,
) -> ResolvedMeasure:
    return ResolvedMeasure(
        name=name,
        aggregation=aggregation,
        expression=FunctionCall(
            name=aggregation.upper(), args=[ColumnRef(name="AMOUNT", table="Orders")]
        ),
        grain_override=grain_override or GrainOverride(mode=GrainMode.FIXED, include=["Region"]),
        effective_grain=effective_grain if effective_grain is not None else ["Region"],
    )


def _make_ast(
    dim_names: list[str] | None = None,
    measure_names: list[str] | None = None,
) -> Select:
    if dim_names is None:
        dim_names = ["Region", "Product"]
    if measure_names is None:
        measure_names = ["Revenue"]
    columns: list[AliasedExpr] = []
    for d in dim_names:
        columns.append(AliasedExpr(expr=ColumnRef(name=d.upper(), table="Orders"), alias=d))
    for m in measure_names:
        columns.append(
            AliasedExpr(
                expr=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
                alias=m,
            )
        )
    return Select(
        columns=columns,
        from_=From(source="WAREHOUSE.PUBLIC.ORDERS", alias="Orders"),
        group_by=[ColumnRef(name=d.upper(), table="Orders") for d in dim_names],
    )


class TestGrainOverrideWrap:
    def test_partition_by_single_dim(self) -> None:
        ast = _make_ast(measure_names=["Region Revenue"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[_make_grain_measure()],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        assert len(result.ctes) == 1
        assert result.ctes[0].name == "base"
        measure_col = result.columns[2]
        assert isinstance(measure_col, AliasedExpr)
        assert measure_col.alias == "Region Revenue"
        assert isinstance(measure_col.expr, WindowFunction)
        assert measure_col.expr.func_name == "SUM"
        assert len(measure_col.expr.partition_by) == 1
        part = measure_col.expr.partition_by[0]
        assert isinstance(part, ColumnRef)
        assert part.name == "Region"

    def test_empty_grain_is_grand_total(self) -> None:
        ast = _make_ast(measure_names=["Grand Total"])
        m = _make_grain_measure(
            name="Grand Total",
            effective_grain=[],
            grain_override=GrainOverride(mode=GrainMode.FIXED),
        )
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[m],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        measure_col = result.columns[2]
        assert isinstance(measure_col, AliasedExpr)
        assert isinstance(measure_col.expr, WindowFunction)
        assert measure_col.expr.partition_by == []

    def test_mixed_regular_and_grain_override(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Region Revenue"])
        regular = ResolvedMeasure(
            name="Revenue",
            aggregation="sum",
            expression=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
        )
        grain = _make_grain_measure()
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[regular, grain],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        assert len(result.columns) == 4
        # Revenue: pass-through
        rev_col = result.columns[2]
        assert isinstance(rev_col, AliasedExpr)
        assert rev_col.alias == "Revenue"
        assert isinstance(rev_col.expr, ColumnRef)
        # Region Revenue: window function with partition
        grain_col = result.columns[3]
        assert isinstance(grain_col, AliasedExpr)
        assert grain_col.alias == "Region Revenue"
        assert isinstance(grain_col.expr, WindowFunction)
        assert len(grain_col.expr.partition_by) == 1

    def test_count_reagg_with_grain(self) -> None:
        ast = _make_ast(measure_names=["Order Count"])
        m = _make_grain_measure(name="Order Count", aggregation="count")
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[m],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        measure_col = result.columns[2]
        assert isinstance(measure_col, AliasedExpr)
        assert isinstance(measure_col.expr, WindowFunction)
        assert measure_col.expr.func_name == "SUM"
        assert len(measure_col.expr.partition_by) == 1

    def test_avg_grain_reagg(self) -> None:
        ast = _make_ast(measure_names=["Avg Revenue"])
        m = _make_grain_measure(name="Avg Revenue", aggregation="avg")
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[m],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        measure_col = result.columns[2]
        assert isinstance(measure_col, AliasedExpr)
        assert isinstance(measure_col.expr, BinaryOp)
        assert measure_col.expr.op == "/"
        assert isinstance(measure_col.expr.left, WindowFunction)
        assert len(measure_col.expr.left.partition_by) == 1
        assert isinstance(measure_col.expr.right, WindowFunction)
        assert len(measure_col.expr.right.partition_by) == 1

    def test_min_reagg_with_grain(self) -> None:
        ast = _make_ast(measure_names=["Min Revenue"])
        m = _make_grain_measure(name="Min Revenue", aggregation="min")
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[m],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        measure_col = result.columns[2]
        assert isinstance(measure_col, AliasedExpr)
        assert isinstance(measure_col.expr, WindowFunction)
        assert measure_col.expr.func_name == "MIN"

    def test_max_reagg_with_grain(self) -> None:
        ast = _make_ast(measure_names=["Max Revenue"])
        m = _make_grain_measure(name="Max Revenue", aggregation="max")
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[m],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        measure_col = result.columns[2]
        assert isinstance(measure_col, AliasedExpr)
        assert isinstance(measure_col.expr, WindowFunction)
        assert measure_col.expr.func_name == "MAX"


class TestGrainOverrideMetric:
    def test_metric_with_grain_component(self) -> None:
        comp_revenue = ResolvedMeasure(
            name="Revenue",
            aggregation="sum",
            expression=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
        )
        comp_region = ResolvedMeasure(
            name="Region Revenue",
            aggregation="sum",
            expression=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
            grain_override=GrainOverride(mode=GrainMode.FIXED, include=["Region"]),
            effective_grain=["Region"],
        )
        metric = ResolvedMeasure(
            name="Revenue Share",
            aggregation="",
            expression=BinaryOp(
                left=ColumnRef(name="Revenue"),
                op="/",
                right=ColumnRef(name="Region Revenue"),
            ),
            component_measures=["Revenue", "Region Revenue"],
            is_expression=True,
        )
        ast = _make_ast(measure_names=["Revenue Share"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim("Region"), _make_dim("Product")],
            measures=[metric],
            base_object="Orders",
            metric_components={
                "Revenue": comp_revenue,
                "Region Revenue": comp_region,
            },
        )
        result = wrap_with_totals(ast, resolved)
        metric_col = result.columns[2]
        assert isinstance(metric_col, AliasedExpr)
        assert metric_col.alias == "Revenue Share"
        assert isinstance(metric_col.expr, BinaryOp)
        # Left: Revenue (non-grain) → ColumnRef
        assert isinstance(metric_col.expr.left, ColumnRef)
        # Right: Region Revenue (grain override) → WindowFunction with partition
        assert isinstance(metric_col.expr.right, WindowFunction)
        assert len(metric_col.expr.right.partition_by) == 1


# -- End-to-end SQL generation ------------------------------------------------


class TestGrainOverrideSQL:
    def test_fixed_grain_sql_postgres(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Revenue", "Region Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.sql_valid, f"SQL validation: {result.warnings}"
        sql = result.sql.upper()
        assert "OVER" in sql
        assert "PARTITION BY" in sql

    def test_grand_total_grain_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Revenue", "Grand Total"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.sql_valid, f"SQL validation: {result.warnings}"
        sql = result.sql.upper()
        assert "OVER ()" in sql or "OVER()" in sql

    def test_relative_exclude_grain_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product", "Category"],
                measures=["Revenue", "Revenue excl Category"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.sql_valid, f"SQL validation: {result.warnings}"
        sql = result.sql.upper()
        assert "PARTITION BY" in sql

    def test_explain_has_grain_overrides(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Revenue", "Region Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.explain is not None
        assert result.explain.has_grain_overrides is True

    def test_all_dialects_produce_valid_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Revenue", "Region Revenue"],
            ),
        )
        dialects = [
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ]
        for dialect in dialects:
            result = pipeline.compile(query, model, dialect)
            assert result.sql_valid, f"{dialect}: {result.warnings}"
            sql = result.sql.upper()
            assert "OVER" in sql, f"{dialect}: missing OVER in SQL"

    def test_metric_with_grain_component_sql(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Product"],
                measures=["Revenue Share"],
            ),
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.sql_valid, f"SQL validation: {result.warnings}"
        sql = result.sql.upper()
        assert "PARTITION BY" in sql


# -- Parser/resolver tests for grain YAML parsing -----------------------------


class TestGrainYamlParsing:
    def test_unknown_grain_dimension_error(self) -> None:
        yaml = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
dimensions:
  Region:
    dataObject: Orders
    column: Amount
measures:
  Bad Grain:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED
      include: [NonExistent]
"""
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(yaml)
        model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        assert any(e.code == "UNKNOWN_GRAIN_DIMENSION" for e in result.errors)

    def test_total_and_grain_mutual_exclusion_yaml(self) -> None:
        yaml = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
dimensions:
  Region:
    dataObject: Orders
    column: Amount
measures:
  Bad Measure:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    total: true
    grain:
      mode: FIXED
"""
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(yaml)
        model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        error_messages = " ".join(e.message for e in result.errors)
        assert "mutually exclusive" in error_messages.lower() or "MEASURE_PARSE_ERROR" in " ".join(
            e.code for e in result.errors
        )

    def test_fixed_with_exclude_validation_yaml(self) -> None:
        yaml = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
dimensions:
  Region:
    dataObject: Orders
    column: Amount
measures:
  Bad Grain:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED
      exclude: [Region]
"""
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(yaml)
        model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        error_messages = " ".join(e.message for e in result.errors)
        assert "FIXED" in error_messages or "MEASURE_PARSE_ERROR" in " ".join(
            e.code for e in result.errors
        )
