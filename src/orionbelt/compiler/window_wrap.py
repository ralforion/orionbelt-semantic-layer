"""Wrapper CTE for ``MetricType.WINDOW`` — rank, lag, lead, ntile, first/last value.

These are single-row-output window functions (no aggregation frame). The wrap
mirrors :mod:`orionbelt.compiler.cumulative_wrap`: the planner output becomes
a base CTE, and the outer query applies the window functions over the
already-aggregated rows.

Window metrics never resize the result set — every row gets one ranking,
prior-value, or bucket assignment. They compose naturally with ``DERIVED``
metrics, so the outer query exposes them by their declared metric name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    Literal,
    OrderByItem,
    Select,
    WindowFunction,
)
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.type_resolver import (
    resolve_measure_data_type,
    resolve_metric_data_type,
)
from orionbelt.models.semantic import WindowFunctionKind

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import SemanticModel


_WINDOW_FUNCTION_NAMES: dict[WindowFunctionKind, str] = {
    WindowFunctionKind.RANK: "RANK",
    WindowFunctionKind.DENSE_RANK: "DENSE_RANK",
    WindowFunctionKind.ROW_NUMBER: "ROW_NUMBER",
    WindowFunctionKind.NTILE: "NTILE",
    WindowFunctionKind.LAG: "LAG",
    WindowFunctionKind.LEAD: "LEAD",
    WindowFunctionKind.FIRST_VALUE: "FIRST_VALUE",
    WindowFunctionKind.LAST_VALUE: "LAST_VALUE",
}


def _build_window_call(measure: ResolvedMeasure) -> WindowFunction:
    """Build the window-function AST node for a ``MetricType.WINDOW`` metric."""
    assert measure.window_function is not None
    kind = measure.window_function
    func_name = _WINDOW_FUNCTION_NAMES[kind]

    base_ref: Expr | None = (
        ColumnRef(name=measure.window_base_measure) if measure.window_base_measure else None
    )
    desc = measure.window_order_direction.lower() == "desc"

    args: list[Expr] = []
    order_by: list[OrderByItem] = []

    if kind in {WindowFunctionKind.RANK, WindowFunctionKind.DENSE_RANK}:
        # RANK() ordered by the base measure (or the time dimension if no measure)
        if base_ref is not None:
            order_by = [OrderByItem(expr=base_ref, desc=desc)]
        elif measure.window_time_dimension:
            order_by = [OrderByItem(expr=ColumnRef(name=measure.window_time_dimension), desc=desc)]
    elif kind == WindowFunctionKind.ROW_NUMBER:
        # ROW_NUMBER() — order by the measure if given, otherwise by time dim
        if base_ref is not None:
            order_by = [OrderByItem(expr=base_ref, desc=desc)]
        elif measure.window_time_dimension:
            order_by = [OrderByItem(expr=ColumnRef(name=measure.window_time_dimension), desc=desc)]
    elif kind == WindowFunctionKind.NTILE:
        assert measure.window_buckets is not None
        args = [Literal.number(measure.window_buckets)]
        if base_ref is not None:
            order_by = [OrderByItem(expr=base_ref, desc=desc)]
        elif measure.window_time_dimension:
            order_by = [OrderByItem(expr=ColumnRef(name=measure.window_time_dimension), desc=desc)]
    elif kind in {WindowFunctionKind.LAG, WindowFunctionKind.LEAD}:
        assert base_ref is not None
        assert measure.window_offset is not None
        assert measure.window_time_dimension is not None
        args = [base_ref, Literal.number(measure.window_offset)]
        if measure.window_default_value is not None:
            args.append(Literal(value=measure.window_default_value))
        # LAG/LEAD always order ascending by time (semantics: prior/next in time)
        order_by = [OrderByItem(expr=ColumnRef(name=measure.window_time_dimension), desc=False)]
    elif kind in {WindowFunctionKind.FIRST_VALUE, WindowFunctionKind.LAST_VALUE}:
        assert base_ref is not None
        args = [base_ref]
        if measure.window_time_dimension:
            order_by = [OrderByItem(expr=ColumnRef(name=measure.window_time_dimension), desc=desc)]

    partition_by: list[Expr] = [
        ColumnRef(name=dim_name) for dim_name in measure.window_partition_by
    ]

    return WindowFunction(
        func_name=func_name,
        args=args,
        partition_by=partition_by,
        order_by=order_by,
    )


def _apply_metric_cast(
    expr: Expr,
    metric_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Wrap a window expression with the metric's declared dataType cast.

    Same shape as ``cumulative_wrap._apply_metric_cast``: honours an
    explicit ``dataType:`` on the window metric so the projected column
    matches what the model declares (avoids INT/FLOAT confusion when a
    user declares ``dataType: integer`` on a RANK metric).
    """
    if model is None or dialect is None:
        return expr
    metric = model.metrics.get(metric_name)
    if metric is None:
        return expr
    resolved_type = resolve_metric_data_type(metric, model.settings)
    if resolved_type is None:
        return expr
    return dialect.cast_to_obml_type(expr, resolved_type)


def _apply_measure_cast(
    expr: Expr,
    measure_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Wrap the base aggregate with the base measure's declared dataType cast.

    Mirrors ``cumulative_wrap._apply_measure_cast``: the ``window_base``
    CTE must carry the same precision the base measure declares, so a
    ``LAG`` / ``LEAD`` / ``RANK`` over a ``decimal(18, 2)`` measure
    operates on the cast value rather than the uncast aggregate. No-op
    when either ``model`` or ``dialect`` is missing or the measure has
    no resolvable declared type.
    """
    if model is None or dialect is None:
        return expr
    base_meas = model.measures.get(measure_name)
    if base_meas is None:
        return expr
    resolved_type = resolve_measure_data_type(base_meas, model.settings)
    if resolved_type is None:
        return expr
    return dialect.cast_to_obml_type(expr, resolved_type)


def _get_alias(expr: Expr) -> str | None:
    if isinstance(expr, AliasedExpr):
        return expr.alias
    return None


def _ddm_window_components(
    measure: ResolvedMeasure,
    metric_components: dict[str, ResolvedMeasure],
) -> list[ResolvedMeasure]:
    """Return the window-metric components this derived measure references.

    A "deferred derived metric" (DDM) is a non-window measure whose
    ``component_measures`` include at least one window metric — the
    derived expression cannot be computed inside the CTE because the
    window function lives in the outer SELECT.
    """
    if measure.is_window or not measure.component_measures:
        return []
    out: list[ResolvedMeasure] = []
    for comp_name in measure.component_measures:
        comp = metric_components.get(comp_name)
        if comp is not None and comp.is_window:
            out.append(comp)
    return out


def window_pass_applies(resolved: ResolvedQuery) -> bool:
    """True when :func:`wrap_with_window` will transform the AST.

    The wrap runs when a window metric is selected directly *or* when a
    derived metric in the SELECT transitively references one (a DDM). This
    is the single source of truth for the window pass's ``applies``
    predicate — the wrapper's own guard delegates to it.
    """
    if any(m.is_window for m in resolved.measures):
        return True
    return any(_ddm_window_components(m, resolved.metric_components) for m in resolved.measures)


def _substitute_for_outer(
    expr: Expr,
    metric_components: dict[str, ResolvedMeasure],
    direct_measure_names: set[str],
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Substitute metric refs for use in the OUTER SELECT post-wrap.

    The DDM's expression carries bare ``ColumnRef(name=<component>)``
    references — at the outer SELECT level we need:

    * Window-metric refs → the inline ``LAG`` / ``RANK`` / etc. call,
      computed against the base measure projected by the CTE.
    * Non-window refs → a bare ``ColumnRef`` to the measure's alias,
      which the CTE projects (the outer SELECT picks it up by name).
    """
    if isinstance(expr, ColumnRef) and expr.table is None and expr.name in metric_components:
        comp = metric_components[expr.name]
        if comp.is_window:
            win_expr = _build_window_call(comp)
            return _apply_metric_cast(win_expr, comp.name, model, dialect)
        return ColumnRef(name=expr.name)
    if isinstance(expr, BinaryOp):
        new_left = _substitute_for_outer(
            expr.left, metric_components, direct_measure_names, model, dialect
        )
        new_right = _substitute_for_outer(
            expr.right, metric_components, direct_measure_names, model, dialect
        )
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    return expr


def wrap_with_window(
    ast: Select,
    resolved: ResolvedQuery,
    *,
    model: SemanticModel | None = None,
    dialect: Dialect | None = None,
) -> Select:
    """Wrap a planner AST with a CTE + outer query for ``MetricType.WINDOW`` metrics.

    Runs after ``cumulative_wrap`` so window metrics can reference cumulative
    or derived metrics declared in the same model (the outer wrap selects
    every measure / metric alias from the base CTE by name).

    Returns ``ast`` unchanged when no window metrics are present and no
    derived metric in the SELECT transitively references one.
    """
    direct_window_measures: list[ResolvedMeasure] = [m for m in resolved.measures if m.is_window]

    # Find derived metrics that *transitively* reference a window metric.
    # Even if no window metric is directly in the SELECT, the wrap must
    # run so the inline LAG / RANK / etc. expression can be assembled at
    # the outer level — without this, the substituted derived expression
    # carries an unbound reference to the window's base measure.
    ddm_window_refs: dict[str, list[ResolvedMeasure]] = {}
    for m in resolved.measures:
        comps = _ddm_window_components(m, resolved.metric_components)
        if comps:
            ddm_window_refs[m.name] = comps

    if not window_pass_applies(resolved):
        return ast

    # Every window metric the wrap needs to expose — direct + transitive.
    effective_window: dict[str, ResolvedMeasure] = {m.name: m for m in direct_window_measures}
    for comps in ddm_window_refs.values():
        for c in comps:
            effective_window.setdefault(c.name, c)

    direct_measure_names = {m.name for m in resolved.measures if not m.component_measures}
    window_names = set(effective_window)
    ddm_names = set(ddm_window_refs)

    # --- Build base CTE columns ---
    base_columns: list[Expr] = []
    for col_node in ast.columns:
        alias = _get_alias(col_node)
        # Direct window-metric column: drop here; the outer SELECT emits
        # the LAG/RANK call; the CTE only carries its BASE measure.
        if alias and alias in window_names and alias not in ddm_names:
            win_metric = effective_window[alias]
            base_name = win_metric.window_base_measure
            if base_name and base_name not in direct_measure_names:
                comp = resolved.metric_components.get(base_name)
                if comp:
                    already_in_base = any(_get_alias(c) == base_name for c in base_columns)
                    if not already_in_base:
                        comp_expr = _apply_measure_cast(comp.expression, comp.name, model, dialect)
                        base_columns.append(AliasedExpr(expr=comp_expr, alias=comp.name))
        elif alias and alias in ddm_names:
            # DDM: drop the (incorrectly-pre-substituted) column; compute
            # it at the outer level. Make sure each window component's
            # base measure lands in the CTE.
            for comp in ddm_window_refs[alias]:
                base_name = comp.window_base_measure
                if not base_name or base_name in direct_measure_names:
                    continue
                base_comp = resolved.metric_components.get(base_name)
                if base_comp is None:
                    continue
                already_in_base = any(_get_alias(c) == base_name for c in base_columns)
                if already_in_base:
                    continue
                comp_expr = _apply_measure_cast(
                    base_comp.expression, base_comp.name, model, dialect
                )
                base_columns.append(AliasedExpr(expr=comp_expr, alias=base_comp.name))
        else:
            base_columns.append(col_node)

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
        grouping=ast.grouping,
    )

    cte_name = "window_base"
    base_cte = CTE(name=cte_name, query=base_cte_query)

    # --- Build outer SELECT ---
    outer_columns: list[Expr] = []

    for dim in resolved.dimensions:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))

    for m in resolved.measures:
        if m.is_window:
            window_expr: Expr = _build_window_call(m)
            window_expr = _apply_metric_cast(window_expr, m.name, model, dialect)
            outer_columns.append(AliasedExpr(expr=window_expr, alias=m.name))
        elif m.name in ddm_names:
            # DDM: substitute the parsed metric expression so every
            # window component becomes an inline window-function call
            # and every non-window component becomes a bare ColumnRef
            # to the CTE-projected column. The OUTER SELECT computes
            # the DDM directly; no third CTE layer needed.
            ddm_expr = _substitute_for_outer(
                m.expression,
                resolved.metric_components,
                direct_measure_names,
                model,
                dialect,
            )
            if model is not None and dialect is not None:
                metric = model.metrics.get(m.name)
                if metric is not None:
                    resolved_type = resolve_metric_data_type(metric, model.settings)
                    if resolved_type is not None:
                        ddm_expr = dialect.cast_to_obml_type(ddm_expr, resolved_type)
            outer_columns.append(AliasedExpr(expr=ddm_expr, alias=m.name))
        else:
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))

    # --- ORDER BY remapping ---
    outer_order_by = _build_outer_order_by(resolved)

    all_ctes = list(ast.ctes) + [base_cte]

    return Select(
        columns=outer_columns,
        from_=From(source=cte_name, alias=cte_name),
        joins=[],
        where=None,
        group_by=[],
        having=None,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _build_outer_order_by(resolved: ResolvedQuery) -> list[OrderByItem]:
    """Build ORDER BY using dimension/measure alias names for the outer CTE query."""
    from orionbelt.compiler.star import _nulls_last

    col_to_dim: dict[tuple[str, str | None], str] = {
        (d.source_column, d.object_name): d.name for d in resolved.dimensions
    }
    order_by: list[OrderByItem] = []
    for expr, desc, nulls in resolved.order_by_exprs:
        nl = _nulls_last(nulls)
        if isinstance(expr, Literal):
            order_by.append(OrderByItem(expr=expr, desc=desc, nulls_last=nl))
        elif isinstance(expr, ColumnRef):
            dim_name = col_to_dim.get((expr.name, expr.table))
            name = dim_name if dim_name else expr.name
            order_by.append(OrderByItem(expr=ColumnRef(name=name), desc=desc, nulls_last=nl))
        else:
            matched = False
            for m in resolved.measures:
                if m.expression == expr:
                    order_by.append(
                        OrderByItem(expr=ColumnRef(name=m.name), desc=desc, nulls_last=nl)
                    )
                    matched = True
                    break
            if not matched:
                order_by.append(OrderByItem(expr=expr, desc=desc, nulls_last=nl))
    return order_by


__all__ = ["wrap_with_window"]
