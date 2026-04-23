"""Wrapper CTE for total and grain-override measures using window functions.

When a measure has ``total: true`` or a ``grain`` override, the per-group
aggregate must be re-aggregated via ``AGG(x) OVER (PARTITION BY ...)``.
For grand totals the partition is empty (``OVER ()``); for grain overrides
the partition contains the effective grain dimensions.

Because window functions cannot coexist with ``GROUP BY`` on pre-grouped
rows, we wrap the planner output in a CTE and apply window functions in
an outer query.

Re-aggregation mapping (outer window function per aggregation type):

| Original       | Window Re-agg            | Notes                          |
|----------------|--------------------------|--------------------------------|
| SUM            | SUM(x) OVER (...)        | sum of per-group sums          |
| COUNT          | SUM(x) OVER (...)        | sum of per-group counts        |
| COUNT_DISTINCT | SUM(x) OVER (...)        | approximation (may overcount)  |
| MIN            | MIN(x) OVER (...)        | min of per-group mins          |
| MAX            | MAX(x) OVER (...)        | max of per-group maxes         |
| AVG            | SUM(s)/SUM(c) OVER (...) | exact via sum+count helpers    |
"""

from __future__ import annotations

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Literal,
    OrderByItem,
    Select,
    WindowFunction,
)
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery

_UNSUPPORTED_TOTAL_AGGS = frozenset({"MEDIAN", "MODE", "LISTAGG", "ANY_VALUE"})


def _reagg_func(aggregation: str) -> str:
    """Return the outer window function name for a given aggregation."""
    agg = aggregation.upper()
    if agg in _UNSUPPORTED_TOTAL_AGGS:
        raise ValueError(
            f"Aggregation '{agg}' does not support total: true "
            "(cannot be re-aggregated via window functions)"
        )
    if agg in ("SUM", "COUNT", "COUNT_DISTINCT", "AVG"):
        return "SUM"
    if agg == "MIN":
        return "MIN"
    if agg == "MAX":
        return "MAX"
    return "SUM"


def _needs_window_wrap(measure: ResolvedMeasure) -> bool:
    """Check if a measure needs window wrapping (total or grain override).

    Measures with filter_context are handled by filter_wrap and skipped here.
    """
    if measure.filter_context is not None:
        return False
    return measure.total or measure.grain_override is not None


def _is_avg_total(measure: ResolvedMeasure) -> bool:
    """Check if a measure is an AVG total/grain-override (needs sum+count helpers)."""
    return _needs_window_wrap(measure) and measure.aggregation.upper() == "AVG"


def _avg_sum_alias(name: str) -> str:
    return f"{name}__sum"


def _avg_count_alias(name: str) -> str:
    return f"{name}__count"


def _partition_by_exprs(measure: ResolvedMeasure) -> list[Expr]:
    """Build PARTITION BY column refs from effective_grain (empty = grand total)."""
    if measure.effective_grain:
        return [ColumnRef(name=d) for d in measure.effective_grain]
    return []


def _build_total_window(measure: ResolvedMeasure) -> Expr:
    """Build a window function for a total/grain-override measure's outer column."""
    partition_by = _partition_by_exprs(measure)
    if _is_avg_total(measure):
        return BinaryOp(
            left=WindowFunction(
                func_name="SUM",
                args=[ColumnRef(name=_avg_sum_alias(measure.name))],
                partition_by=partition_by,
            ),
            op="/",
            right=WindowFunction(
                func_name="SUM",
                args=[ColumnRef(name=_avg_count_alias(measure.name))],
                partition_by=partition_by,
            ),
        )
    reagg = _reagg_func(measure.aggregation)
    return WindowFunction(
        func_name=reagg,
        args=[ColumnRef(name=measure.name)],
        partition_by=partition_by,
    )


def _collect_total_names(resolved: ResolvedQuery) -> set[str]:
    """Collect names of all measures that need window wrapping (direct + metric components)."""
    names: set[str] = set()
    for m in resolved.measures:
        if _needs_window_wrap(m):
            names.add(m.name)
        for comp_name in m.component_measures:
            comp = resolved.metric_components.get(comp_name)
            if comp and _needs_window_wrap(comp):
                names.add(comp.name)
    return names


def _metrics_with_total_components(resolved: ResolvedQuery) -> set[str]:
    """Identify metrics that reference at least one window-wrapped component."""
    names: set[str] = set()
    for m in resolved.measures:
        if not m.component_measures:
            continue
        for comp_name in m.component_measures:
            comp = resolved.metric_components.get(comp_name)
            if comp and _needs_window_wrap(comp):
                names.add(m.name)
                break
    return names


def _substitute_metric_refs(
    expr: Expr,
    resolved: ResolvedQuery,
    total_names: set[str],
) -> Expr:
    """Walk a metric AST and replace ColumnRef placeholders.

    Non-total components → ColumnRef (pass-through from base CTE).
    Total components → WindowFunction (re-aggregation).
    """
    if isinstance(expr, ColumnRef) and expr.table is None:
        comp = resolved.metric_components.get(expr.name)
        if comp:
            if _needs_window_wrap(comp):
                return _build_total_window(comp)
            return ColumnRef(name=comp.name)
    if isinstance(expr, BinaryOp):
        new_left = _substitute_metric_refs(expr.left, resolved, total_names)
        new_right = _substitute_metric_refs(expr.right, resolved, total_names)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    return expr


def wrap_with_totals(ast: Select, resolved: ResolvedQuery) -> Select:
    """Wrap a planner AST with a CTE + outer query for total measures.

    If no totals are present, returns ``ast`` unchanged.
    """
    if not resolved.has_totals:
        return ast

    total_names = _collect_total_names(resolved)
    decompose_metrics = _metrics_with_total_components(resolved)

    if not total_names and not decompose_metrics:
        return ast

    # --- Build base CTE columns from the planner's AST columns ---
    base_columns: list[Expr] = []
    # Track which component measures are already present as direct measures
    direct_measure_names = {m.name for m in resolved.measures if not m.component_measures}

    for col_node in ast.columns:
        alias = _get_alias(col_node)
        if alias and alias in decompose_metrics:
            # Replace metric column with its individual component columns
            metric = next(m for m in resolved.measures if m.name == alias)
            for comp_name in metric.component_measures:
                if comp_name in direct_measure_names:
                    continue  # Already present as a direct measure
                comp = resolved.metric_components.get(comp_name)
                if comp:
                    if _is_avg_total(comp):
                        # AVG total needs sum + count helper columns
                        base_columns.append(_build_avg_helpers_base_col(comp, "sum"))
                        base_columns.append(_build_avg_helpers_base_col(comp, "count"))
                    else:
                        base_columns.append(AliasedExpr(expr=comp.expression, alias=comp.name))
        elif alias and _is_avg_window_wrap_by_name(alias, resolved):
            # AVG total/grain-override direct measure: replace with sum + count helpers
            measure = next(m for m in resolved.measures if m.name == alias)
            base_columns.append(_build_avg_helpers_base_col(measure, "sum"))
            base_columns.append(_build_avg_helpers_base_col(measure, "count"))
        else:
            base_columns.append(col_node)

    # --- Build base CTE: planner's AST with modified columns, no ORDER BY/LIMIT ---
    base_cte_query = Select(
        columns=base_columns,
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

    base_cte = CTE(name="base", query=base_cte_query)

    # --- Build outer SELECT ---
    outer_columns: list[Expr] = []

    # Dimensions: pass-through
    for dim in resolved.dimensions:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))

    # Measures
    for m in resolved.measures:
        if m.component_measures:
            # Metric
            if m.name in decompose_metrics:
                # Rebuild expression with window functions for total components
                metric_expr = _substitute_metric_refs(m.expression, resolved, total_names)
                outer_columns.append(AliasedExpr(expr=metric_expr, alias=m.name))
            else:
                # Metric without total components: pass-through
                outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))
        elif _needs_window_wrap(m):
            # Total or grain-override measure: window function
            outer_columns.append(AliasedExpr(expr=_build_total_window(m), alias=m.name))
        else:
            # Regular measure: pass-through
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))

    # --- ORDER BY remapping: use CTE aliases instead of raw expressions ---
    outer_order_by = _remap_order_by(ast.order_by, resolved)

    # --- Assemble final Select ---
    all_ctes = list(ast.ctes) + [base_cte]

    return Select(
        columns=outer_columns,
        from_=_from_cte("base"),
        joins=[],
        where=None,
        group_by=[],
        having=None,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _get_alias(expr: Expr) -> str | None:
    """Extract the alias from an AliasedExpr, or None."""
    if isinstance(expr, AliasedExpr):
        return expr.alias
    return None


def _from_cte(name: str) -> From:
    """Build a FROM clause referencing a CTE."""
    return From(source=name, alias=name)


def _remap_order_by(
    order_by: list[OrderByItem],
    resolved: ResolvedQuery,
) -> list[OrderByItem]:
    """Remap ORDER BY items to reference CTE column aliases.

    The planner's ORDER BY may contain raw table-qualified expressions
    (e.g. ``SUM("Line Items"."l_extendedprice" * ...)``).  In the outer
    query these tables don't exist — only the base CTE's column aliases.
    """
    dim_map: dict[tuple[str, str | None], str] = {
        (d.source_column, d.object_name): d.name for d in resolved.dimensions
    }
    measure_exprs: list[tuple[Expr, str]] = [(m.expression, m.name) for m in resolved.measures]

    result: list[OrderByItem] = []
    for ob in order_by:
        remapped = _remap_single_order_expr(ob.expr, dim_map, measure_exprs)
        result.append(OrderByItem(expr=remapped, desc=ob.desc, nulls_last=ob.nulls_last))
    return result


def _remap_single_order_expr(
    expr: Expr,
    dim_map: dict[tuple[str, str | None], str],
    measure_exprs: list[tuple[Expr, str]],
) -> Expr:
    """Remap one ORDER BY expression to use the CTE alias."""
    if isinstance(expr, ColumnRef) and expr.table is not None:
        key = (expr.name, expr.table)
        if key in dim_map:
            return ColumnRef(name=dim_map[key])
        return ColumnRef(name=expr.name)
    for meas_expr, name in measure_exprs:
        if expr is meas_expr or expr == meas_expr:
            return ColumnRef(name=name)
    if isinstance(expr, Literal):
        return expr
    return expr


def _is_avg_window_wrap_by_name(name: str, resolved: ResolvedQuery) -> bool:
    """Check if a direct measure with given name is an AVG total/grain-override."""
    for m in resolved.measures:
        if m.name == name and not m.component_measures:
            return _is_avg_total(m)
    return False


def _build_avg_helpers_base_col(measure: ResolvedMeasure, kind: str) -> AliasedExpr:
    """Build a SUM or COUNT base CTE column for an AVG total measure.

    For AVG(expr), we need:
    - SUM(expr) AS "name__sum"
    - COUNT(expr) AS "name__count"
    """
    if isinstance(measure.expression, FunctionCall) and measure.expression.args:
        inner_args = list(measure.expression.args)
    else:
        # Fallback: use Literal(1) — an AVG measure with no args is unusual
        # but using the measure alias as a column ref would be invalid SQL.
        inner_args = [Literal.number(1)]

    if kind == "sum":
        return AliasedExpr(
            expr=FunctionCall(name="SUM", args=inner_args),
            alias=_avg_sum_alias(measure.name),
        )
    else:
        return AliasedExpr(
            expr=FunctionCall(name="COUNT", args=inner_args),
            alias=_avg_count_alias(measure.name),
        )
