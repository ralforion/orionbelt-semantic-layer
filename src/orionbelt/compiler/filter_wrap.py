"""CTE isolation for measures with filterContext overrides.

When a measure has ``filterContext``, it needs its own query context (different
WHERE clause).  This module wraps the planner output with:

- A ``main`` CTE for inline measures (no filter context)
- Isolated CTEs for filter-contexted measures (grouped by effective filter set)
- An outer SELECT that JOINs all CTEs together

Strategy selection per the design doc:

| Grain          | Filter context | Result                         |
|----------------|----------------|--------------------------------|
| Same as query  | Different      | CTE + LEFT JOIN on all dims    |
| Subset of dims | Different      | CTE + LEFT JOIN on subset dims |
| Empty (scalar) | Different      | CTE + CROSS JOIN               |
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    Join,
    JoinType,
    OrderByItem,
    Select,
)
from orionbelt.compiler.expr_rewrite import map_column_refs
from orionbelt.compiler.filters import build_filter_expr
from orionbelt.compiler.having_hoist import windowed_aliases
from orionbelt.compiler.metric_expansion import metric_leaf_components, metric_over_components
from orionbelt.compiler.resolution import (
    ResolvedFilter,
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import FilterOperator, QueryFilter
from orionbelt.models.semantic import (
    DataObject,
    FilterContext,
    FilterContextMode,
    SemanticModel,
)

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect


def _compute_effective_filters(
    fc: FilterContext,
    where_filters: list[ResolvedFilter],
) -> list[ResolvedFilter]:
    """Determine which WHERE filters apply given a filter context override."""
    if fc.mode == FilterContextMode.FIXED:
        effective: list[ResolvedFilter] = []
    else:
        effective = list(where_filters)

    if fc.exclude:
        exclude_set = set(fc.exclude)
        effective = [f for f in effective if not (f.referenced_fields & exclude_set)]

    if fc.keep_only:
        keep_set = set(fc.keep_only)
        effective = [f for f in effective if f.referenced_fields & keep_set]

    return effective


def _resolve_include_filters(
    fc: FilterContext,
    model: SemanticModel,
) -> list[ResolvedFilter]:
    """Resolve filterContext.include items to physical filter expressions."""
    results: list[ResolvedFilter] = []
    errors: list[SemanticError] = []
    # Route every column reference through ``make_column_expr`` so static
    # / filterContext include items work on computed (``expression:``)
    # columns — without this the empty ``code:`` slot leaks into the SQL
    # and the database errors on the zero-length identifier.
    from orionbelt.compiler.resolution import make_column_expr

    for incl in fc.include:
        dim = model.dimensions.get(incl.field)
        if dim:
            obj = model.data_objects.get(dim.view)
            if obj and dim.column in obj.columns:
                col_expr: Expr = make_column_expr(model, dim.view, dim.column)
                try:
                    op = FilterOperator(incl.op)
                except ValueError:
                    continue
                qf = QueryFilter(field=incl.field, op=op, value=incl.value)
                filter_expr = build_filter_expr(col_expr, qf, errors)
                if filter_expr:
                    results.append(
                        ResolvedFilter(
                            expression=filter_expr,
                            referenced_fields=frozenset({incl.field}),
                        )
                    )
        elif "." in incl.field:
            parts = incl.field.split(".", 1)
            obj_name, col_name = parts[0].strip(), parts[1].strip()
            obj = model.data_objects.get(obj_name)
            if obj and col_name in obj.columns:
                col_expr = make_column_expr(model, obj_name, col_name)
                try:
                    op = FilterOperator(incl.op)
                except ValueError:
                    continue
                qf = QueryFilter(field=incl.field, op=op, value=incl.value)
                filter_expr = build_filter_expr(col_expr, qf, errors)
                if filter_expr:
                    results.append(
                        ResolvedFilter(
                            expression=filter_expr,
                            referenced_fields=frozenset({incl.field}),
                        )
                    )
    return results


def _effective_grain_dims(measure: ResolvedMeasure, query_dims: list[str]) -> list[str]:
    """The dimensions a filter-isolated measure's own CTE groups by.

    ``total: true`` is the same claim as ``grain: {mode: FIXED}`` with nothing
    kept - a grand total - but only the grain override reaches
    ``effective_grain``. Read from the query grain instead, the measure came
    back per group, and ``total_wrap`` was never going to correct it: it skips
    filter-contexted measures on the grounds that this wrapper owns them.
    """
    if measure.total:
        return []
    if measure.effective_grain is not None:
        return measure.effective_grain
    return query_dims


def _filter_key(
    fc: FilterContext,
    effective_grain: list[str],
) -> tuple[str, ...]:
    """Build a hashable key for grouping measures with identical filter context + grain."""
    parts: list[str] = [fc.mode.value]
    parts.append("excl:" + ",".join(sorted(fc.exclude)))
    parts.append("keep:" + ",".join(sorted(fc.keep_only)))
    parts.append("incl:" + ",".join(f"{i.field}:{i.op}:{i.value}" for i in fc.include))
    parts.append("grain:" + ",".join(effective_grain))
    return tuple(parts)


def _get_alias(expr: Expr) -> str | None:
    if isinstance(expr, AliasedExpr):
        return expr.alias
    return None


def _combine_where(filters: list[ResolvedFilter]) -> Expr | None:
    """Combine a list of resolved filters into a single WHERE expression."""
    if not filters:
        return None
    combined: Expr = filters[0].expression
    for f in filters[1:]:
        combined = BinaryOp(left=combined, op="AND", right=f.expression)
    return combined


def _substitute_aliases(expr: Expr, by_alias: dict[str, Expr]) -> Expr:
    """Replace every bare (unqualified) ``ColumnRef`` that names a mapped alias.

    A HAVING predicate references a measure as a table-less ``ColumnRef``
    carrying its name, so this turns one into whichever expression the target
    scope projects under that name.
    """
    return map_column_refs(
        expr, lambda ref: by_alias.get(ref.name, ref) if ref.table is None else ref
    )


def _combine_exprs(exprs: Iterable[Expr]) -> Expr | None:
    """AND a run of predicates together, or ``None`` if there are none."""
    combined: Expr | None = None
    for expr in exprs:
        combined = expr if combined is None else BinaryOp(left=combined, op="AND", right=expr)
    return combined


def _component_expr(measure: ResolvedMeasure, resolved: ResolvedQuery) -> Expr:
    """The aggregate to project for *measure*, in a CTE of this wrapper's own.

    ``projected_expressions`` when the plan rewrote it - an anchored measure
    reads a conformed subquery rather than its own fact - and the resolved
    expression otherwise. Same rule every pass that re-projects an aggregate
    follows; see ``ResolvedQuery.projected_expressions``.
    """
    return resolved.projected_expressions.get(measure.name, measure.expression)


def wrap_with_filter_context(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
) -> Select:
    """Wrap planner AST with CTEs for filter-isolated measures.

    Returns ``ast`` unchanged if no measures have filter context.
    """
    leaf_components = {
        m.name: metric_leaf_components(m, resolved.metric_components) for m in resolved.measures
    }
    # A metric whose formula reads a filter-contexted measure. The planner
    # inlined that component's aggregate into the metric's one column, where the
    # query's WHERE applies to it like any other - so the column is dropped and
    # the formula rebuilt in the outer query over the components' own columns,
    # each read from whichever CTE computed it.
    split_metrics = {
        m.name: m
        for m in resolved.measures
        if m.component_measures
        and any(c.filter_context is not None for c in leaf_components[m.name])
    }

    # Isolated: the filter-contexted measures the query selects, plus the ones
    # it only reads through a metric. Deduplicated by name, since selecting a
    # measure *and* a metric over it must still compute it once.
    isolated: list[ResolvedMeasure] = [m for m in resolved.measures if m.filter_context is not None]
    seen_isolated = {m.name for m in isolated}
    for metric in split_metrics.values():
        for comp in leaf_components[metric.name]:
            if comp.filter_context is not None and comp.name not in seen_isolated:
                seen_isolated.add(comp.name)
                isolated.append(comp)
    if not isolated:
        return ast

    query_dim_names = [d.name for d in resolved.dimensions]

    # Group isolated measures by their filter context + grain key
    groups: dict[tuple[str, ...], list[ResolvedMeasure]] = {}
    for m in isolated:
        assert m.filter_context is not None
        grain = _effective_grain_dims(m, query_dim_names)
        key = _filter_key(m.filter_context, grain)
        groups.setdefault(key, []).append(m)

    inline_measures = [m for m in resolved.measures if m.filter_context is None]
    inline_names = {m.name for m in inline_measures if m.name not in split_metrics}

    # --- Build main CTE from planner AST ---
    main_columns: list[Expr] = []
    for col_node in ast.columns:
        alias = _get_alias(col_node)
        dim_names = {d.name for d in resolved.dimensions}
        if alias and alias not in inline_names and alias not in dim_names:
            continue
        main_columns.append(col_node)

    # A split metric's column is gone, so the components it carried that stay
    # in ``main`` have to be projected in their own right.
    main_aliases = {a for c in main_columns if (a := _get_alias(c)) is not None}
    for metric in split_metrics.values():
        for comp in leaf_components[metric.name]:
            if comp.filter_context is not None or comp.name in main_aliases:
                continue
            main_columns.append(AliasedExpr(expr=_component_expr(comp, resolved), alias=comp.name))
            main_aliases.add(comp.name)

    # A HAVING predicate is evaluated inside the CTE the planner built. For an
    # isolated measure that CTE is ``main``, under the query's filters and over
    # an aggregate that is not the measure's - the predicate read the wrong
    # value and silently kept the wrong groups. Those predicates move to the
    # outer query instead, where every measure is one column of a CTE. The rest
    # stay where the planner put them, expression for expression.
    hoisted = {m.name for m in isolated} | set(split_metrics)
    outer_having = [hf for hf in resolved.having_filters if hf.referenced_fields & hoisted]
    main_having = ast.having
    if outer_having:
        planner_exprs = {
            alias: col.expr
            for col in ast.columns
            if isinstance(col, AliasedExpr) and (alias := _get_alias(col)) is not None
        }
        # A predicate on a measure a *later* wrapper windows stays withheld:
        # the planner left it out deliberately and ``PASS_HAVING_WINDOW``
        # applies it once over the windowed rows.
        deferred = hoisted | windowed_aliases(resolved)
        main_having = _combine_exprs(
            _substitute_aliases(hf.expression, planner_exprs)
            for hf in resolved.having_filters
            if not hf.referenced_fields & deferred
        )

    main_cte_query = Select(
        columns=main_columns,
        from_=ast.from_,
        joins=ast.joins,
        where=ast.where,
        group_by=ast.group_by,
        having=main_having,
        order_by=[],
        limit=None,
        offset=None,
        ctes=[],
        grouping=ast.grouping,
    )
    # With no dimensions and every measure isolated, ``main`` projects nothing
    # and degenerates to ``SELECT *`` - one row per fact row, which the CROSS
    # JOIN below then multiplies the scalar result by. The isolated CTEs are
    # each one row at this grain and stand on their own, so drop it.
    keep_main = bool(main_columns)

    main_cte = CTE(name="main", query=main_cte_query)

    # --- Build isolated CTEs ---
    all_ctes = list(ast.ctes) + ([main_cte] if keep_main else [])
    isolated_cte_info: list[tuple[str, list[ResolvedMeasure], list[str]]] = []

    for idx, (_key, measure_group) in enumerate(groups.items()):
        representative = measure_group[0]
        assert representative.filter_context is not None
        fc = representative.filter_context
        grain = _effective_grain_dims(representative, query_dim_names)

        effective_where_filters = _compute_effective_filters(fc, resolved.where_filters)
        include_filters = _resolve_include_filters(fc, model)
        all_filters = effective_where_filters + include_filters

        # Build CTE columns: grain dimensions + measures
        cte_columns: list[Expr] = []
        for dim in resolved.dimensions:
            if dim.name in grain:
                col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
                if dim.grain and dialect:
                    col = dialect.render_time_grain(col, dim.grain)
                cte_columns.append(AliasedExpr(expr=col, alias=dim.name))

        for m in measure_group:
            cte_columns.append(AliasedExpr(expr=_component_expr(m, resolved), alias=m.name))

        # GROUP BY
        cte_group_by: list[Expr] = []
        for dim in resolved.dimensions:
            if dim.name in grain:
                gb_col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
                if dim.grain and dialect:
                    gb_col = dialect.render_time_grain(gb_col, dim.grain)
                cte_group_by.append(gb_col)

        cte_name = f"fc_{idx}"
        cte_query = Select(
            columns=cte_columns,
            from_=ast.from_,
            joins=list(ast.joins),
            where=_combine_where(all_filters),
            group_by=cte_group_by,
            having=None,
            order_by=[],
            limit=None,
            offset=None,
            ctes=[],
        )
        all_ctes.append(CTE(name=cte_name, query=cte_query))
        isolated_cte_info.append((cte_name, measure_group, grain))

    # --- Build outer SELECT ---
    outer_columns: list[Expr] = []
    for dim in resolved.dimensions:
        outer_columns.append(
            AliasedExpr(expr=ColumnRef(name=dim.name, table="main"), alias=dim.name)
        )
    cte_of: dict[str, str] = {
        m.name: cte_name for cte_name, measure_group, _ in isolated_cte_info for m in measure_group
    }
    for m in inline_measures:
        if m.name in split_metrics:
            outer_columns.append(
                AliasedExpr(
                    expr=metric_over_components(
                        m,
                        resolved.metric_components,
                        lambda name: ColumnRef(name=name, table=cte_of.get(name, "main")),
                        model,
                        dialect,
                    ),
                    alias=m.name,
                )
            )
            continue
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name, table="main"), alias=m.name))
    selected_names = {m.name for m in resolved.measures}
    for cte_name, measure_group, _ in isolated_cte_info:
        for m in measure_group:
            # A component the query reads only through a metric is computed in
            # this CTE but is not one of the query's columns - the metric that
            # reads it is.
            if m.name not in selected_names:
                continue
            outer_columns.append(
                AliasedExpr(expr=ColumnRef(name=m.name, table=cte_name), alias=m.name)
            )

    # --- JOINs from main to isolated CTEs ---
    # Without ``main`` the first isolated CTE anchors the FROM and the rest
    # cross-join onto it, which is what they would have done anyway.
    anchor = "main" if keep_main else isolated_cte_info[0][0]
    outer_joins: list[Join] = []
    for cte_name, _, grain in isolated_cte_info:
        if cte_name == anchor:
            continue
        if not grain:
            outer_joins.append(
                Join(
                    join_type=JoinType.CROSS,
                    source=cte_name,
                    alias=cte_name,
                    on=None,
                )
            )
        else:
            on_parts: list[Expr] = []
            for dim_name in grain:
                on_parts.append(
                    BinaryOp(
                        left=ColumnRef(name=dim_name, table=anchor),
                        op="=",
                        right=ColumnRef(name=dim_name, table=cte_name),
                    )
                )
            on_expr: Expr = on_parts[0]
            for part in on_parts[1:]:
                on_expr = BinaryOp(left=on_expr, op="AND", right=part)
            outer_joins.append(
                Join(
                    join_type=JoinType.LEFT,
                    source=cte_name,
                    alias=cte_name,
                    on=on_expr,
                )
            )

    # The hoisted predicates, rebuilt over the outer projection. One row per
    # query grain here, so filtering it is exactly what HAVING would have done
    # had the value been available where the planner put the predicate.
    outer_exprs = {
        alias: col.expr
        for col in outer_columns
        if isinstance(col, AliasedExpr) and (alias := _get_alias(col)) is not None
    }
    outer_where = _combine_exprs(
        _substitute_aliases(hf.expression, outer_exprs) for hf in outer_having
    )

    # Recorded for the wrappers that run after this one. Each of them
    # re-projects some measure's aggregate into a CTE of its own, and their CTE
    # selects from what this wrapper built - where the fact tables the resolved
    # expressions name are gone, and every component is one column of ``main``
    # or of an isolated CTE. Without this, a metric mixing a filterContext
    # component with a ``total`` one had the second rebuilt as
    # ``SUM("Sales"."AMOUNT")`` against a FROM that no longer has ``Sales``.
    for cte_name, measure_group, _ in isolated_cte_info:
        for m in measure_group:
            resolved.projected_expressions[m.name] = ColumnRef(name=m.name, table=cte_name)
    for metric in split_metrics.values():
        for comp in leaf_components[metric.name]:
            if comp.filter_context is None:
                resolved.projected_expressions[comp.name] = ColumnRef(name=comp.name, table=anchor)

    # --- ORDER BY remapping: resolve to CTE aliases ---
    dim_map: dict[tuple[str, str | None], str] = {
        (d.source_column, d.object_name): d.name for d in resolved.dimensions
    }
    dim_exprs: list[tuple[Expr, str]] = [
        (make_column_expr(model, d.object_name, d.column_name), d.name) for d in resolved.dimensions
    ]
    measure_exprs: list[tuple[Expr, str]] = [(m.expression, m.name) for m in resolved.measures]

    def order_ref(name: str) -> Expr:
        """Where to read *name* from in the outer query.

        A rebuilt metric has no CTE column at all - it is assembled in this
        query's own projection - so it orders by its select alias.
        """
        if name in split_metrics:
            return ColumnRef(name=name)
        return ColumnRef(name=name, table=cte_of.get(name, anchor))

    outer_order_by: list[OrderByItem] = []
    for ob in ast.order_by:
        remapped = _remap_fc_order_expr(
            ob.expr,
            dim_map,
            dim_exprs,
            measure_exprs,
            order_ref,
            lambda name: ColumnRef(name=name, table=anchor),
        )
        outer_order_by.append(OrderByItem(expr=remapped, desc=ob.desc, nulls_last=ob.nulls_last))

    return Select(
        columns=outer_columns,
        from_=From(source=anchor, alias=anchor),
        joins=outer_joins,
        where=outer_where,
        group_by=[],
        having=None,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _remap_fc_order_expr(
    expr: Expr,
    dim_map: dict[tuple[str, str | None], str],
    dim_exprs: list[tuple[Expr, str]],
    measure_exprs: list[tuple[Expr, str]],
    order_ref: Callable[[str], Expr],
    dim_ref: Callable[[str], Expr],
) -> Expr:
    """Remap one ORDER BY expression for the filter-context outer query.

    The planner ordered by the raw expression; here every value is a column of
    one of the CTEs, and *which* CTE depends on the value - a dimension is in
    the anchor, a measure is in whichever CTE computed it. Ordering by the
    measure's aggregate as written would read the wrong scan.
    """
    if isinstance(expr, ColumnRef) and expr.table is not None:
        key = (expr.name, expr.table)
        if key in dim_map:
            return dim_ref(dim_map[key])
        return dim_ref(expr.name)
    # A computed dimension orders by its inlined expression, not by a column
    # reference, so it is matched structurally against the same expression the
    # CTE projected under its alias.
    for dim_expr, name in dim_exprs:
        if expr == dim_expr:
            return dim_ref(name)
    for meas_expr, name in measure_exprs:
        if expr is meas_expr or expr == meas_expr:
            return order_ref(name)
    return expr
