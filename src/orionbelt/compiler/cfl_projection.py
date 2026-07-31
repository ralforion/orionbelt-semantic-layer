"""CFL outer-projection / null-padding / measure-ref helpers.

Extracted from ``cfl.py``. Functions that need planner state take the
:class:`~orionbelt.compiler.cfl.CFLPlanner` instance as their first argument
(``planner``); the rest are pure helpers. The planner keeps thin delegators so
its public surface is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    OrderByItem,
)
from orionbelt.compiler.expr_rewrite import collect_column_refs
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.metric_expansion import (
    expand_metric_expression,
    metric_leaf_components,
)
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

    * **Passthrough aggregates** (COUNT / COUNT_DISTINCT / LISTAGG) — the
      inner column projection is the *raw column itself* (e.g. ``complid``,
      a text ID). The outer ``COUNT(DISTINCT ...)`` happily counts any
      type, but each CFL leg's column must agree on a type for
      ``UNION ALL``. Padding with the declared aggregate output type
      (BIGINT) trips strict-typed engines (Postgres / MySQL / strict
      ClickHouse) when the source column is text. Pad with the
      source column's abstract type instead.

      LISTAGG belongs here for a sharper reason than type-strictness: its
      declared result type resolves to the numeric default, so treating it
      as a numeric aggregate cast the *source* column to it and emitted
      ``CAST("Sales"."product" AS FLOAT)`` over a text column — SQL that
      every engine rejects at execution ("Could not convert string 'widget'
      to FLOAT"). Any LISTAGG in a multi-fact query was broken this way.

    For multi-field measures (e.g. ``COUNT(a, b)``), per-column
    abstract types are used regardless of aggregation kind.
    """
    model_measure = model.effective_measures.get(measure.name)
    if not model_measure:
        return None
    agg = (model_measure.aggregation or "").lower()
    is_passthrough_style = agg in ("count", "count_distinct", "listagg")
    # Multi-field measures: per-column abstract_type for each slot.
    if len(model_measure.columns) > 1:
        if field_idx < len(model_measure.columns):
            ref = model_measure.columns[field_idx]
            obj = model.data_objects.get(ref.view) if ref.view else None
            if obj and ref.column in obj.columns:
                return obj.columns[ref.column].abstract_type.value
        return model_measure.result_type.value
    # Single-/zero-column passthrough: pad with the source column's
    # native type so UNION ALL legs agree (raw column, not aggregate).
    if is_passthrough_style and len(model_measure.columns) == 1:
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


def build_outer_aggregate(measure: ResolvedMeasure, cte_name: str) -> FunctionCall:
    """Rebuild a measure's aggregate over the composite CTE.

    The legs project the aggregate's *input*, so the outer query re-applies the
    aggregation. Everything the aggregate carries besides its argument has to be
    reapplied here too, or it is silently lost: ``DISTINCT``, the LISTAGG
    ``separator`` (which otherwise falls back to ``","``, dropping a declared
    ``delimiter``), and a ``withinGroup`` ordering, which reads the sort key the
    legs carried through the union under :func:`within_group_cte_alias`.

    Shared by the measure and metric projections. They each rebuilt this
    independently before, and the metric copy silently lacked the ordering and
    separator handling.
    """
    agg = measure.aggregation.upper()
    distinct = False
    if agg == "COUNT_DISTINCT":
        agg = "COUNT"
        distinct = True
    source = measure.expression if isinstance(measure.expression, FunctionCall) else None
    if source is not None and source.distinct:
        distinct = True
    order_by: list[OrderByItem] = []
    if source is not None and source.order_by:
        # A self-ordering aggregate orders by the very column it aggregates.
        # Point the outer ORDER BY at the measure's own column rather than a
        # separate sort-key column: ClickHouse and Databricks cannot express
        # cross-column ordering and compare the two renderings textually, so a
        # distinct ``__wg`` alias would make them reject an aggregate they
        # support perfectly well on the single-fact path.
        order_by = [
            OrderByItem(
                expr=ColumnRef(
                    name=(
                        measure.name
                        if is_self_ordered(measure)
                        else within_group_cte_alias(measure.name)
                    ),
                    table=cte_name,
                ),
                desc=source.order_by[0].desc,
            )
        ]
    return FunctionCall(
        name=agg,
        args=[ColumnRef(name=measure.name, table=cte_name)],
        distinct=distinct,
        order_by=order_by,
        separator=source.separator if source is not None else None,
    )


def is_self_ordered(measure: ResolvedMeasure) -> bool:
    """Whether an ordered aggregate sorts by the same column it aggregates.

    ``LISTAGG(x) WITHIN GROUP (ORDER BY x)`` needs no separate sort-key column
    in the union: the value column already carries it. Dialects that can only
    express self-ordering (ClickHouse's ``arraySort``, Databricks' ``sort_array``)
    depend on this, since they compare the aggregate's argument and its order
    key by rendered SQL.
    """
    source = measure.expression
    if not isinstance(source, FunctionCall) or not source.order_by or not source.args:
        return False
    return source.order_by[0].expr == source.args[0]


def within_group_cte_alias(measure_name: str) -> str:
    """CTE column name carrying an ordered aggregate's sort key.

    The CFL outer query re-aggregates over ``composite_01``, so a
    ``withinGroup`` clause can only survive if its sort column is carried
    through the union as a column of its own. The leg that owns the measure
    projects it under this alias; sibling legs NULL-pad it.
    """
    return f"{measure_name}__wg"


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
    """Substitute a metric's component refs with outer aggregations.

    Every reference in the formula becomes ``AGG("cte"."component")`` over the
    UNION ALL composite, wherever it sits in the expression — inside a function
    call (``... / NULLIF(other, 0)``), a ``CASE``, or a nested derived metric,
    whose own formula is expanded here rather than left as a bare label that
    would bind against a column no leg projects.
    """

    def value_of(comp: ResolvedMeasure) -> Expr:
        return build_outer_aggregate(comp, cte_name)

    return expand_metric_expression(expr, resolved.metric_components, value_of)


def collect_table_refs(expr: Expr, tables: set[str]) -> None:
    """Collect the table name of every ``ColumnRef`` anywhere in *expr*.

    Delegates to the complete AST walk in :mod:`expr_rewrite` rather than
    enumerating node types here. The hand-rolled version this replaces covered
    only a handful of nodes and silently returned nothing for the rest, so a
    computed column expanding to ``CASE`` or ``CAST`` contributed no tables:
    the leg then projected ``CASE WHEN "Reason"."severity" > 2 ...`` over a
    FROM that never joined ``Reason``. It also skipped an aggregate's
    ``order_by``, which a ``withinGroup`` sort key lives in.
    """
    refs: list[ColumnRef] = []
    collect_column_refs(expr, refs)
    tables.update(ref.table for ref in refs if ref.table)


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


def _single_leg_root(
    objects: set[str],
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> str | None:
    """The object one leg can be rooted at to cover *objects*, if there is one.

    A measure reading several objects is not automatically cross-fact: when the
    objects are joined — ``{[Sales].[Qty]} * {[Products].[Price]}`` over a
    many-to-one — one leg rooted at the common ancestor reaches them all and
    projects the expression exactly as the star planner would. Only when no
    single root reaches every object are they genuinely independent facts, which
    is the case the caller hands to ``cross_fact``.
    """
    if len(objects) <= 1:
        return next(iter(objects), None)
    graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)
    root = graph.find_common_root(objects)
    if root and objects <= (graph.descendants(root) | {root}):
        return root
    return None


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
            # Metric: add each component measure to its source object, following
            # nested derived metrics — those are expanded into the same formula,
            # so it is their leaves that need a leg.
            for comp in metric_leaf_components(measure, resolved.metric_components):
                if comp.name in seen:
                    continue
                seen.add(comp.name)
                model_measure = model.effective_measures.get(comp.name)
                if model_measure and model_measure.columns:
                    comp_objects = {f.view for f in model_measure.columns if f.view}
                else:
                    # Declared as an expression rather than ``columns:`` — the
                    # source objects are in the aggregate's own table references.
                    # Falling back to the base object put the component in the
                    # wrong leg, which then projected a column its FROM has not
                    # joined.
                    comp_objects = set()
                    planner._collect_table_refs(comp.expression, comp_objects)
                root = _single_leg_root(comp_objects, resolved, model)
                if root is None:
                    # Reads facts no single leg reaches — same treatment as a
                    # cross-fact direct measure: give every object a leg and let
                    # ``_plan_union_all`` distribute the fields.
                    cross_fact.append(comp)
                    for obj in comp_objects:
                        groups.setdefault(obj, [])
                    continue
                groups.setdefault(root or resolved.base_object, []).append(comp)
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
            root = _single_leg_root(field_objects, resolved, model) if field_objects else None
            if field_objects and root is None:
                # Cross-fact multi-field measure: ensure each
                # involved object has a leg, but don't assign
                # the measure to any single group.
                cross_fact.append(measure)
                for obj in field_objects:
                    groups.setdefault(obj, [])
            else:
                groups.setdefault(root or resolved.base_object, []).append(measure)

    return groups, cross_fact
