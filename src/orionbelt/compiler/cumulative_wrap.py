"""Wrapper CTE for cumulative (running/rolling/grain-to-date) metrics.

Cumulative metrics are window functions applied to already-aggregated measures,
ordered by a time dimension. Three core patterns:

| Pattern        | SQL Frame                                           |
|----------------|-----------------------------------------------------|
| Running total  | ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW    |
| Rolling window | ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW          |
| Grain-to-date  | PARTITION BY TRUNC(grain) + ROWS UNBOUNDED PRECEDING |

The wrapper follows the same CTE pattern as ``total_wrap.py``:
the planner output becomes a base CTE, and an outer query applies
the cumulative window functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Literal,
    OrderByItem,
    Select,
    WindowFrame,
    WindowFunction,
)
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.type_resolver import (
    resolve_measure_data_type,
    resolve_metric_data_type,
)
from orionbelt.models.semantic import CumulativeAggType, GrainToDate

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import SemanticModel

# Map CumulativeAggType → SQL window function name
_CUMULATIVE_AGG_MAP: dict[CumulativeAggType, str] = {
    CumulativeAggType.SUM: "SUM",
    CumulativeAggType.AVG: "AVG",
    CumulativeAggType.MIN: "MIN",
    CumulativeAggType.MAX: "MAX",
    CumulativeAggType.COUNT: "COUNT",
}

# Map GrainToDate → DATE_TRUNC grain string
_GRAIN_TRUNC_MAP: dict[GrainToDate, str] = {
    GrainToDate.YEAR: "year",
    GrainToDate.QUARTER: "quarter",
    GrainToDate.MONTH: "month",
    GrainToDate.WEEK: "week",
}


def _build_cumulative_window(
    measure: ResolvedMeasure,
    time_dim_name: str,
) -> Expr:
    """Build the window function expression for a cumulative metric.

    ``cumulative_partition_by`` adds additional ``PARTITION BY`` keys beyond
    the implicit ``DATE_TRUNC(grain, time)`` partition for grain-to-date.
    For rolling and running totals, partition keys are the dimension names
    only — the underlying ``base`` CTE already exposes them as bare aliases.
    """
    func_name = _CUMULATIVE_AGG_MAP[measure.cumulative_type]
    base_ref = ColumnRef(name=measure.cumulative_measure or measure.name)
    time_ref = ColumnRef(name=time_dim_name)
    order_by = [OrderByItem(expr=time_ref)]
    extra_partitions: list[Expr] = [
        ColumnRef(name=dim_name) for dim_name in measure.cumulative_partition_by
    ]

    if measure.cumulative_grain_to_date is not None:
        # Grain-to-date: PARTITION BY DATE_TRUNC(grain, time_dim), unbounded frame
        grain = _GRAIN_TRUNC_MAP[measure.cumulative_grain_to_date]
        partition_expr = FunctionCall(
            name="DATE_TRUNC",
            args=[Literal.string(grain), time_ref],
        )
        return WindowFunction(
            func_name=func_name,
            args=[base_ref],
            partition_by=[partition_expr, *extra_partitions],
            order_by=order_by,
            frame=WindowFrame(
                mode="ROWS",
                start="UNBOUNDED PRECEDING",
                end="CURRENT ROW",
            ),
        )

    if measure.cumulative_window is not None:
        # Rolling window: ROWS BETWEEN (window-1) PRECEDING AND CURRENT ROW
        preceding = measure.cumulative_window - 1
        return WindowFunction(
            func_name=func_name,
            args=[base_ref],
            partition_by=extra_partitions,
            order_by=order_by,
            frame=WindowFrame(
                mode="ROWS",
                start=f"{preceding} PRECEDING",
                end="CURRENT ROW",
            ),
        )

    # Running total (unbounded): ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    return WindowFunction(
        func_name=func_name,
        args=[base_ref],
        partition_by=extra_partitions,
        order_by=order_by,
        frame=WindowFrame(
            mode="ROWS",
            start="UNBOUNDED PRECEDING",
            end="CURRENT ROW",
        ),
    )


def wrap_with_cumulative(
    ast: Select,
    resolved: ResolvedQuery,
    *,
    model: SemanticModel | None = None,
    dialect: Dialect | None = None,
) -> Select:
    """Wrap a planner AST with a CTE + outer query for cumulative metrics.

    If no cumulative metrics are present, returns ``ast`` unchanged.

    ``model`` and ``dialect`` are used to wrap the base measure expression
    (inside ``cumulative_base``) and the outer windowed aggregate with
    ``CAST`` to the declared dataType, mirroring what ``star.py`` and
    ``cfl.py`` already do for non-cumulative measures. Without those
    casts, the cumulative_base CTE carries unwrapped DOUBLE values and
    accumulates float drift through the window — a precision bug that
    silently violates the metric's declared ``dataType``. Both kwargs
    are optional so legacy callers continue to compile (without the
    casts).
    """
    if not resolved.has_cumulative:
        return ast

    # Find time dimension names used by cumulative metrics
    cumulative_measures: list[ResolvedMeasure] = [m for m in resolved.measures if m.is_cumulative]

    # --- Build base CTE columns from the planner's AST ---
    # We need to decompose cumulative metrics into their base measure components
    # in the base CTE, then apply window functions in the outer query.
    direct_measure_names = {m.name for m in resolved.measures if not m.component_measures}
    cumulative_names = {m.name for m in cumulative_measures}

    base_columns: list[Expr] = []
    for col_node in ast.columns:
        alias = _get_alias(col_node)
        if alias and alias in cumulative_names:
            # Replace cumulative metric with its base measure component
            cum_metric = next(m for m in cumulative_measures if m.name == alias)
            comp_name = cum_metric.cumulative_measure
            if comp_name and comp_name not in direct_measure_names:
                comp = resolved.metric_components.get(comp_name)
                if comp:
                    # Only add the component if not already present
                    already_in_base = any(_get_alias(c) == comp_name for c in base_columns)
                    if not already_in_base:
                        comp_expr = _apply_measure_cast(comp.expression, comp.name, model, dialect)
                        base_columns.append(AliasedExpr(expr=comp_expr, alias=comp.name))
            # If the base measure is already a direct measure, it's already in the columns
        else:
            base_columns.append(col_node)

    # --- Build base CTE ---
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

    cte_name = "cumulative_base"
    base_cte = CTE(name=cte_name, query=base_cte_query)

    # --- Build outer SELECT ---
    outer_columns: list[Expr] = []

    # Dimensions: pass-through
    for dim in resolved.dimensions:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))

    # Measures and metrics
    for m in resolved.measures:
        if m.is_cumulative:
            # Cumulative metric: build window function
            assert m.cumulative_time_dimension is not None
            window_expr: Expr = _build_cumulative_window(m, m.cumulative_time_dimension)
            window_expr = _apply_metric_cast(window_expr, m.name, model, dialect)
            outer_columns.append(AliasedExpr(expr=window_expr, alias=m.name))
        else:
            # Regular measure or derived metric: pass-through
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))

    # --- ORDER BY remapping ---
    outer_order_by = _build_outer_order_by(resolved)

    # --- Assemble final Select ---
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


def _apply_measure_cast(
    expr: Expr,
    measure_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Wrap an aggregate expression with the base measure's declared dataType cast.

    Mirrors the cast pattern in ``compiler/star.py`` so the
    ``cumulative_base`` CTE carries the same precision the metric
    declares. No-op if either ``model`` or ``dialect`` is None, or if
    the measure has no resolvable declared type.
    """
    if model is None or dialect is None:
        return expr
    base_meas = model.effective_measures.get(measure_name)
    if base_meas is None:
        return expr
    resolved_type = resolve_measure_data_type(base_meas, model.settings)
    if resolved_type is None:
        return expr
    return dialect.cast_to_obml_type(expr, resolved_type)


def _apply_metric_cast(
    expr: Expr,
    metric_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Wrap a windowed cumulative expression with the metric's declared dataType cast.

    Same shape as ``_apply_measure_cast`` but resolves the type from the
    cumulative *metric* definition (e.g. ``Cumulative Sales`` declares
    ``decimal(18, 2)``). Without this the outer windowed aggregate
    propagates the underlying input type, which for DOUBLE columns
    introduces last-bit float drift.
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


def _get_alias(expr: Expr) -> str | None:
    """Extract the alias from an AliasedExpr, or None."""
    if isinstance(expr, AliasedExpr):
        return expr.alias
    return None


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
            # Measure expression — find matching measure by expression equality
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
