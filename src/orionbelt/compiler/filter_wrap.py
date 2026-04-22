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

from collections.abc import Callable
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
from orionbelt.compiler.filters import build_filter_expr
from orionbelt.compiler.resolution import ResolvedFilter, ResolvedMeasure, ResolvedQuery
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
    for incl in fc.include:
        dim = model.dimensions.get(incl.field)
        if dim:
            obj = model.data_objects.get(dim.view)
            if obj and dim.column in obj.columns:
                source = obj.columns[dim.column].code
                col_expr: Expr = ColumnRef(name=source, table=dim.view)
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
                source = obj.columns[col_name].code
                col_expr = ColumnRef(name=source, table=obj_name)
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
    """Get the effective grain dimensions for a filter-isolated measure."""
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
    isolated = [m for m in resolved.measures if m.filter_context is not None]
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
    inline_names = {m.name for m in inline_measures}

    # --- Build main CTE from planner AST ---
    main_columns: list[Expr] = []
    for col_node in ast.columns:
        alias = _get_alias(col_node)
        dim_names = {d.name for d in resolved.dimensions}
        if alias and alias not in inline_names and alias not in dim_names:
            continue
        main_columns.append(col_node)

    main_cte_query = Select(
        columns=main_columns,
        from_=ast.from_,
        joins=ast.joins,
        where=ast.where,
        group_by=ast.group_by,
        having=ast.having,
        order_by=[],
        limit=None,
        offset=None,
        ctes=[],
    )
    main_cte = CTE(name="main", query=main_cte_query)

    # --- Build isolated CTEs ---
    all_ctes = list(ast.ctes) + [main_cte]
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
                col: Expr = ColumnRef(name=dim.source_column, table=dim.object_name)
                if dim.grain and dialect:
                    col = dialect.render_time_grain(col, dim.grain)
                cte_columns.append(AliasedExpr(expr=col, alias=dim.name))

        for m in measure_group:
            cte_columns.append(AliasedExpr(expr=m.expression, alias=m.name))

        # GROUP BY
        cte_group_by: list[Expr] = []
        for dim in resolved.dimensions:
            if dim.name in grain:
                gb_col: Expr = ColumnRef(name=dim.source_column, table=dim.object_name)
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
    for m in inline_measures:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name, table="main"), alias=m.name))
    for cte_name, measure_group, _ in isolated_cte_info:
        for m in measure_group:
            outer_columns.append(
                AliasedExpr(expr=ColumnRef(name=m.name, table=cte_name), alias=m.name)
            )

    # --- JOINs from main to isolated CTEs ---
    outer_joins: list[Join] = []
    for cte_name, _, grain in isolated_cte_info:
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
                        left=ColumnRef(name=dim_name, table="main"),
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

    # --- ORDER BY remapping ---
    outer_order_by: list[OrderByItem] = []
    for ob in ast.order_by:
        remapped = ob.expr
        if isinstance(remapped, ColumnRef) and remapped.table is not None:
            remapped = ColumnRef(name=remapped.name, table="main")
        outer_order_by.append(OrderByItem(expr=remapped, desc=ob.desc, nulls_last=ob.nulls_last))

    return Select(
        columns=outer_columns,
        from_=From(source="main", alias="main"),
        joins=outer_joins,
        where=None,
        group_by=[],
        having=None,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )
