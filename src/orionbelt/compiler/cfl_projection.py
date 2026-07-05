"""CFL outer-projection / null-padding / measure-ref helpers.

Extracted from ``cfl.py``. Functions that need planner state take the
:class:`~orionbelt.compiler.cfl.CFLPlanner` instance as their first argument
(``planner``); the rest are pure helpers. The planner keeps thin delegators so
its public surface is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    InList,
    IsNull,
    Literal,
    RelativeDateRange,
    UnaryOp,
)
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.resolution import (
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
)
from orionbelt.compiler.type_resolver import resolve_measure_data_type
from orionbelt.dialect.base import Dialect
from orionbelt.models.semantic import (
    SemanticModel,
)

if TYPE_CHECKING:
    from orionbelt.compiler.cfl import CFLPlanner


def expand_cfl_measure_refs(expr: Expr, measure_exprs: dict[str, Expr]) -> Expr:
    """Replace bare ColumnRef aliases in HAVING with their full aggregate expressions.

    Recurses through ``BinaryOp`` and ``FunctionCall.args`` so a metric
    formula like ``{Total Refunds} / NULLIF({Total Sales}, 0)`` correctly
    inlines both refs in HAVING / outer-SELECT contexts.
    """
    if isinstance(expr, ColumnRef) and expr.table is None and expr.name in measure_exprs:
        return measure_exprs[expr.name]
    if isinstance(expr, BinaryOp):
        new_left = expand_cfl_measure_refs(expr.left, measure_exprs)
        new_right = expand_cfl_measure_refs(expr.right, measure_exprs)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    if isinstance(expr, FunctionCall):
        new_args = [expand_cfl_measure_refs(a, measure_exprs) for a in expr.args]
        if any(n is not o for n, o in zip(new_args, expr.args, strict=True)):
            return FunctionCall(
                name=expr.name,
                args=new_args,
                distinct=expr.distinct,
                order_by=expr.order_by,
                separator=expr.separator,
            )
    return expr


def group_dimensions_into_legs(
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> dict[str, list[ResolvedMeasure]]:
    """Group dimensions into CFL legs for dimension-only queries.

    For each dimension, find the fact/bridge table that can reach it
    via directed join paths, and use that as the leg's key object.
    Returns empty measure lists per leg (dimension-only, no aggregates).
    """
    graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)
    legs: dict[str, list[ResolvedMeasure]] = {}
    assigned: set[str] = set()

    # Build a lookup: for each dimension object, which fact tables can reach it?
    dim_objects = {d.object_name for d in resolved.dimensions}
    fact_candidates: list[tuple[str, set[str]]] = []
    for obj_name, obj in model.data_objects.items():
        if not obj.joins:
            continue
        reachable_dims = dim_objects & (graph.descendants(obj_name) | {obj_name})
        if reachable_dims:
            fact_candidates.append((obj_name, reachable_dims))

    # Greedy: pick fact table covering most unassigned dimensions first
    fact_candidates.sort(key=lambda x: (-len(x[1]), x[0]))
    for fact_obj, reachable in fact_candidates:
        covers = reachable - assigned
        if covers:
            legs[fact_obj] = []
            assigned.update(covers)

    return legs


def is_multi_field(measure: ResolvedMeasure) -> bool:
    """Check if a measure has multiple field args (e.g. COUNT(a, b))."""
    return isinstance(measure.expression, FunctionCall) and len(measure.expression.args) > 1


def resolve_null_type_for_field(
    measure: ResolvedMeasure,
    field_idx: int,
    model: SemanticModel,
    dialect: Dialect | None = None,
) -> str | None:
    """Resolve the SQL type for NULL padding in CFL UNION ALL legs.

    Two regimes apply:

    * **Numeric aggregates** (SUM / AVG / MIN / MAX / MEDIAN / etc.) —
      the inner column projection is the *aggregate's input column*, and
      OBSL casts the outer aggregate to the measure's declared
      ``dataType`` (e.g. ``decimal(18, 2)``). Padding with that same
      declared type keeps every CFL leg's column compatible with the
      outer ``SUM``/``AVG`` and avoids ClickHouse's ``Decimal`` +
      ``Float64`` Variant trap (where padding with the column's
      declared OBML ``abstractType: float`` mismatches storage as
      ``Decimal`` and produces ``ILLEGAL_TYPE_OF_ARGUMENT``).

    * **Count-style aggregates** (COUNT / COUNT_DISTINCT) — the inner
      column projection is the *raw column itself* (e.g. ``complid``,
      a text ID). The outer ``COUNT(DISTINCT ...)`` happily counts any
      type, but each CFL leg's column must agree on a type for
      ``UNION ALL``. Padding with the declared aggregate output type
      (BIGINT) trips strict-typed engines (Postgres / MySQL / strict
      ClickHouse) when the source column is text. Pad with the
      source column's abstract type instead.

    For multi-field measures (e.g. ``COUNT(a, b)``), per-column
    abstract types are used regardless of aggregation kind.
    """
    model_measure = model.effective_measures.get(measure.name)
    if not model_measure:
        return None
    agg = (model_measure.aggregation or "").lower()
    is_count_style = agg in ("count", "count_distinct")
    # Multi-field measures: per-column abstract_type for each slot.
    if len(model_measure.columns) > 1:
        if field_idx < len(model_measure.columns):
            ref = model_measure.columns[field_idx]
            obj = model.data_objects.get(ref.view) if ref.view else None
            if obj and ref.column in obj.columns:
                return obj.columns[ref.column].abstract_type.value
        return model_measure.result_type.value
    # Single-/zero-column COUNT-style: pad with the source column's
    # native type so UNION ALL legs agree (raw column, not aggregate).
    if is_count_style and len(model_measure.columns) == 1:
        ref = model_measure.columns[0]
        obj = model.data_objects.get(ref.view) if ref.view else None
        if obj and ref.column in obj.columns:
            return obj.columns[ref.column].abstract_type.value
    # Numeric aggregates: align padding with the outer CAST target.
    if dialect is not None and len(model_measure.columns) <= 1:
        resolved = resolve_measure_data_type(model_measure, model.settings)
        if resolved is not None:
            return dialect.render_obml_type(resolved)
    # Fallback to measure result_type.
    return model_measure.result_type.value


def multi_field_cte_alias(measure_name: str, idx: int) -> str:
    """CTE column name for the *idx*-th field of a multi-field measure."""
    return f"{measure_name}__f{idx}"


def unwrap_aggregation(measure: ResolvedMeasure) -> Expr:
    """Extract the inner expression from an aggregated measure.

    For FunctionCall(SUM, [inner]) → returns inner.
    Falls back to the full expression if not a FunctionCall.
    """
    if isinstance(measure.expression, FunctionCall) and measure.expression.args:
        return measure.expression.args[0]
    return measure.expression


def build_outer_metric_expr(
    planner: CFLPlanner,
    metric: ResolvedMeasure,
    resolved: ResolvedQuery,
    cte_name: str,
) -> Expr:
    """Build the outer query expression for a metric.

    Walks the metric's AST tree and replaces each ColumnRef(measure_name)
    with ``AGG("cte_name"."measure_name")`` using the component measure's
    aggregation. The CTE qualification matters: when the outer SELECT
    also aliases its column ``measure_name`` to ``AGG(...)``, ClickHouse
    resolves a bare ``"measure_name"`` to the sibling alias (the
    aggregate itself) and rejects the resulting nested aggregate as
    ``ILLEGAL_AGGREGATION``. Qualifying with the CTE name forces the
    inner ref to resolve to the raw CTE column.
    """
    return planner._substitute_outer_refs(metric.expression, resolved, cte_name)


def substitute_outer_refs(
    planner: CFLPlanner, expr: Expr, resolved: ResolvedQuery, cte_name: str
) -> Expr:
    """Recursively substitute measure refs with outer aggregations.

    Walks ``BinaryOp`` and ``FunctionCall.args`` so a metric formula
    with embedded SQL functions (e.g. ``... / NULLIF(other, 0)``)
    substitutes refs inside the function call instead of leaving the
    bare label, which would later bind against a non-existent
    column.
    """
    if isinstance(expr, ColumnRef) and expr.table is None:
        comp = resolved.metric_components.get(expr.name)
        if comp:
            agg = comp.aggregation.upper()
            distinct = False
            if agg == "COUNT_DISTINCT":
                agg = "COUNT"
                distinct = True
            if isinstance(comp.expression, FunctionCall) and comp.expression.distinct:
                distinct = True
            return FunctionCall(
                name=agg,
                args=[ColumnRef(name=comp.name, table=cte_name)],
                distinct=distinct,
            )
    if isinstance(expr, BinaryOp):
        new_left = planner._substitute_outer_refs(expr.left, resolved, cte_name)
        new_right = planner._substitute_outer_refs(expr.right, resolved, cte_name)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    if isinstance(expr, FunctionCall):
        new_args = [planner._substitute_outer_refs(a, resolved, cte_name) for a in expr.args]
        if any(n is not o for n, o in zip(new_args, expr.args, strict=True)):
            return FunctionCall(
                name=expr.name,
                args=new_args,
                distinct=expr.distinct,
                order_by=expr.order_by,
                separator=expr.separator,
            )
    return expr


def collect_table_refs(expr: Expr, tables: set[str]) -> None:
    """Recursively collect table names from ColumnRef nodes."""
    if isinstance(expr, ColumnRef) and expr.table:
        tables.add(expr.table)
    elif isinstance(expr, BinaryOp):
        collect_table_refs(expr.left, tables)
        collect_table_refs(expr.right, tables)
    elif isinstance(expr, UnaryOp):
        collect_table_refs(expr.operand, tables)
    elif isinstance(expr, (InList, IsNull, Between)):
        collect_table_refs(expr.expr, tables)
    elif isinstance(expr, RelativeDateRange):
        collect_table_refs(expr.column, tables)
    elif isinstance(expr, FunctionCall):
        for arg in expr.args:
            collect_table_refs(arg, tables)


def remap_cfl_order_by(expr: Expr, resolved: ResolvedQuery, model: SemanticModel) -> Expr:
    """Remap ORDER BY expressions to use CTE aliases for the outer query.

    In CFL, the outer query selects from the composite CTE — original
    table-qualified refs are out of scope.  Remap dimension and measure
    expressions to their CTE alias names. Matches by structural equality
    with each dimension's column expression so computed columns (where
    the source AST is an inlined expression, not a bare ColumnRef) also
    remap correctly.
    """
    for dim in resolved.dimensions:
        if expr == make_column_expr(model, dim.object_name, dim.column_name):
            return ColumnRef(name=dim.name)
    # Measure: match by identity (same expression object)
    for meas in resolved.measures:
        if expr is meas.expression:
            return ColumnRef(name=meas.name)
    # Numeric position — pass through
    return expr


def build_outer_concat_count(
    planner: CFLPlanner,
    measure_name: str,
    n_fields: int,
    agg: str,
    distinct: bool,
    cte_name: str,
) -> Expr:
    """Build ``COUNT(DISTINCT CAST(f0 AS VARCHAR) || '|' || ...)`` for the outer query.

    Each field reference is qualified with *cte_name* so it resolves to
    the raw CTE column rather than any sibling SELECT alias (see
    ``_substitute_outer_refs`` for the alias-shadowing rationale).
    """
    parts: list[Expr] = [
        Cast(
            expr=ColumnRef(
                name=planner._multi_field_cte_alias(measure_name, i),
                table=cte_name,
            ),
            type_name="VARCHAR",
        )
        for i in range(n_fields)
    ]
    concat: Expr = parts[0]
    for part in parts[1:]:
        concat = BinaryOp(
            left=concat,
            op="||",
            right=BinaryOp(
                left=Literal.string("|"),
                op="||",
                right=part,
            ),
        )
    return FunctionCall(name=agg, args=[concat], distinct=distinct)


def group_measures_by_object(
    planner: CFLPlanner,
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> tuple[dict[str, list[ResolvedMeasure]], list[ResolvedMeasure]]:
    """Group measures by their primary source object.

    Returns ``(groups, cross_fact)`` where *cross_fact* contains
    multi-field measures whose fields span multiple objects.
    For metrics, expand their component measures into the grouping
    instead of the metric itself.  Cross-fact measures ensure every
    involved object has a leg, but are not assigned to any single
    group — their individual fields are distributed per-leg by
    ``_plan_union_all``.
    """
    groups: dict[str, list[ResolvedMeasure]] = {}
    cross_fact: list[ResolvedMeasure] = []
    seen: set[str] = set()

    for measure in resolved.measures:
        if measure.component_measures:
            # Metric: add each component measure to its source object
            for comp_name in measure.component_measures:
                if comp_name in seen:
                    continue
                seen.add(comp_name)
                comp = resolved.metric_components.get(comp_name)
                if comp is None:
                    continue
                model_measure = model.effective_measures.get(comp_name)
                if model_measure and model_measure.columns:
                    obj_name = model_measure.columns[0].view or resolved.base_object
                else:
                    obj_name = resolved.base_object
                groups.setdefault(obj_name, []).append(comp)
        else:
            if measure.name in seen:
                continue
            seen.add(measure.name)
            model_measure = model.effective_measures.get(measure.name)
            if not model_measure:
                groups.setdefault(resolved.base_object, []).append(measure)
                continue

            # Collect source objects: from explicit columns or expression AST
            field_objects: set[str]
            if model_measure.columns:
                field_objects = {f.view for f in model_measure.columns if f.view}
            else:
                # Expression-based measure: extract table refs from the AST
                field_objects = set()
                planner._collect_table_refs(measure.expression, field_objects)
            if len(field_objects) > 1:
                # Cross-fact multi-field measure: ensure each
                # involved object has a leg, but don't assign
                # the measure to any single group.
                cross_fact.append(measure)
                for obj in field_objects:
                    groups.setdefault(obj, [])
            elif field_objects:
                obj_name = next(iter(field_objects))
                groups.setdefault(obj_name, []).append(measure)
            else:
                groups.setdefault(resolved.base_object, []).append(measure)

    return groups, cross_fact
