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

from collections.abc import Iterable
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    Select,
    Unnest,
    WindowFrame,
    WindowFunction,
)
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.time_lookback import lookback_query, null_safe_eq, time_filters
from orionbelt.compiler.type_resolver import (
    cast_measure_to_resolved_type,
    resolve_metric_data_type,
)
from orionbelt.compiler.window_wrap import partition_keys, wraps_a_cte
from orionbelt.models.semantic import CumulativeAggType, GrainToDate, TimeGrain

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
    dialect: Dialect | None = None,
    group_dims: list[str] | None = None,
    model: SemanticModel | None = None,
) -> Expr:
    """Build the window function expression for a cumulative metric.

    ``group_dims`` (the query's other dimensions, see :func:`_group_dimensions`)
    and ``cumulative_partition_by`` are ``PARTITION BY`` keys, after the
    implicit ``DATE_TRUNC(grain, time)`` partition for grain-to-date. They are
    dimension names: the underlying ``base`` CTE exposes them as bare aliases.
    """
    func_name = _CUMULATIVE_AGG_MAP[measure.cumulative_type]
    base_ref = ColumnRef(name=measure.cumulative_measure or measure.name)
    time_ref = ColumnRef(name=time_dim_name)
    order_by = [OrderByItem(expr=time_ref)]
    # The query's other dimensions partition the window, so the metric
    # accumulates per group (per country, per product, ...), then any
    # metric-level ``partitionBy`` keys that the query did not already add.
    partition_names = list(dict.fromkeys([*(group_dims or []), *measure.cumulative_partition_by]))
    extra_partitions = partition_keys(partition_names, model, dialect)

    if measure.cumulative_grain_to_date is not None:
        # Grain-to-date: PARTITION BY <truncated time_dim>, unbounded frame.
        # Use the dialect's typed time-grain node (the same one time-grain
        # dimensions use) so each engine emits valid truncation SQL — a hardcoded
        # DATE_TRUNC() fails on engines without it (MySQL) or with different
        # syntax (BigQuery). Fall back to the literal form only for legacy callers
        # that pass no dialect.
        grain = _GRAIN_TRUNC_MAP[measure.cumulative_grain_to_date]
        partition_expr: Expr
        if dialect is not None:
            partition_expr = dialect.render_time_grain(time_ref, TimeGrain(grain))
        else:
            partition_expr = FunctionCall(name="DATE_TRUNC", args=[Literal.string(grain), time_ref])
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


def _group_dimensions(resolved: ResolvedQuery, time_dim_name: str) -> list[str]:
    """The query's dimensions that split the result into groups for a cumulative metric.

    Every selected dimension except the time dimension itself and dimensions
    over the same date column at another grain (``Sales Year`` next to
    ``Sales Month``): those are positions on the time axis, and partitioning
    by them would restart a running total at each year. "Same column" is the
    logical column reached the same way: the physical name is empty for every
    computed column, and one calendar column reached through two join paths
    (``via``) is two time axes.
    """
    time_dim = next((d for d in resolved.dimensions if d.name == time_dim_name), None)
    groups: list[str] = []
    for dim in resolved.dimensions:
        if dim.name == time_dim_name:
            continue
        if time_dim is not None and (dim.object_name, dim.column_name, dim.via) == (
            time_dim.object_name,
            time_dim.column_name,
            time_dim.via,
        ):
            continue
        groups.append(dim.name)
    return groups


def _component_base_column(
    col_node: Expr,
    comp: ResolvedMeasure,
    resolved: ResolvedQuery,
    model: SemanticModel | None,
    dialect: Dialect | None,
    over_cte: bool = False,
) -> AliasedExpr:
    """Project a cumulative metric's base measure into the base CTE.

    Normally that means re-deriving the component's aggregate from the fact
    tables, which is what the planner would have emitted for it.

    That does not survive ``compiler.grain_dedup``. It rewrites the query into
    CTEs, so this wrapper's FROM is the dedup output rather than the fact
    tables, and ``SUM("Sales"."quantity")`` re-projected there fails to bind.
    The metric's own column already holds that aggregate, computed at the query
    grain before the rewrite, so it is re-aliased to the component's name
    instead of rebuilt — the same alias-not-expression rule that lets
    ``total_wrap`` compose.
    """
    # An anchored measure's aggregate reads conformed subquery columns, not the
    # foreign fact's own, so re-deriving it here would name a table this CTE
    # joins a GROUP BY subquery in place of.
    rebuilt = resolved.projected_expressions.get(comp.name, comp.expression)
    # Taking the column by alias is required whenever the input is already a
    # CTE, not only after grain dedup: a CFL composite holds the aggregate too,
    # and neither the fact tables nor an anchored measure's conformed
    # subqueries are in scope out there.
    source = (
        col_node.expr
        if (resolved.dedup_targets or over_cte) and isinstance(col_node, AliasedExpr)
        else rebuilt
    )
    # The declared dataType cast belongs on whichever form is projected. Taking
    # the column by alias without it silently widened the result — a measure
    # declared decimal(18, 2) came back HUGEINT once a deduplicated measure
    # pulled this path.
    return AliasedExpr(
        expr=_apply_measure_cast(source, comp.name, model, dialect),
        alias=comp.name,
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

    cumulative_measures: list[ResolvedMeasure] = [m for m in resolved.measures if m.is_cumulative]

    cte_name = "cumulative_base"
    over_cte = wraps_a_cte(ast)
    base_cte = CTE(name=cte_name, query=_base_query(ast, resolved, model, dialect, over_cte))
    lookbacks = _lookback_windows(
        ast, resolved, cumulative_measures, model, dialect, cte_name, over_cte
    )
    order_by = outer_order_by(resolved, model)

    if not lookbacks:
        outer_columns: list[Expr] = []
        for dim in resolved.dimensions:
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
        for m in resolved.measures:
            if m.is_cumulative:
                window_expr = _window_for(m, resolved, model, dialect)
                window_expr = _apply_metric_cast(window_expr, m.name, model, dialect)
                outer_columns.append(AliasedExpr(expr=window_expr, alias=m.name))
            else:
                outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))
        return Select(
            columns=outer_columns,
            from_=From(source=cte_name, alias=cte_name),
            order_by=order_by,
            limit=ast.limit,
            offset=ast.offset,
            ctes=[*ast.ctes, base_cte],
        )

    # A time filter selects the rows shown; the windows read past it. Each
    # window CTE carries one row per group with the metric's value, and the
    # rows the query asked for pick theirs up by their dimensions.
    joined_name = "cumulative_joined"
    joined_columns: list[Expr] = [
        AliasedExpr(expr=ColumnRef(table=cte_name, name=d.name), alias=d.name)
        for d in resolved.dimensions
    ]
    window_of = {m.name: name for name, _, metrics in lookbacks for m in metrics}
    for m in resolved.measures:
        table = window_of.get(m.name, cte_name)
        joined_columns.append(AliasedExpr(expr=ColumnRef(table=table, name=m.name), alias=m.name))
    joins: list[Join | Unnest] = [
        Join(
            join_type=JoinType.LEFT,
            source=name,
            alias=name,
            on=_all_of(
                null_safe_eq(
                    ColumnRef(table=cte_name, name=d.name), ColumnRef(table=name, name=d.name)
                )
                for d in resolved.dimensions
            ),
        )
        for name, _, _ in lookbacks
    ]
    joined_cte = CTE(
        name=joined_name,
        query=Select(
            columns=joined_columns,
            from_=From(source=cte_name, alias=cte_name),
            joins=joins,
        ),
    )
    outer: list[Expr] = [
        AliasedExpr(expr=ColumnRef(name=d.name), alias=d.name) for d in resolved.dimensions
    ]
    for m in resolved.measures:
        column: Expr = ColumnRef(name=m.name)
        if m.is_cumulative:
            column = _apply_metric_cast(column, m.name, model, dialect)
        outer.append(AliasedExpr(expr=column, alias=m.name))
    return Select(
        columns=outer,
        from_=From(source=joined_name, alias=joined_name),
        order_by=order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=[
            *ast.ctes,
            base_cte,
            *(cte for _, ctes, _ in lookbacks for cte in ctes),
            joined_cte,
        ],
    )


def _base_query(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel | None,
    dialect: Dialect | None,
    over_cte: bool,
) -> Select:
    """The planner output re-projected for the windows: each cumulative metric
    replaced by its base measure, unordered.

    *over_cte* says whether the query being wrapped reads a CTE an earlier pass
    built, in which case the base measure is taken by alias. It is the wrapped
    query's, passed in rather than read off *ast*: a look-back body has its
    CTEs split off, and read from that it looked like the planner's own output
    and re-derived the aggregate over tables it does not join.
    """
    cumulative_names = {m.name for m in resolved.measures if m.is_cumulative}
    direct_measure_names = {m.name for m in resolved.measures if not m.component_measures}
    base_columns: list[Expr] = []
    for col_node in ast.columns:
        alias = _get_alias(col_node)
        if alias and alias in cumulative_names:
            # Replace cumulative metric with its base measure component
            cum_metric = next(m for m in resolved.measures if m.name == alias)
            comp_name = cum_metric.cumulative_measure
            if comp_name and comp_name not in direct_measure_names:
                comp = resolved.metric_components.get(comp_name)
                if comp and not any(_get_alias(c) == comp_name for c in base_columns):
                    base_columns.append(
                        _component_base_column(col_node, comp, resolved, model, dialect, over_cte)
                    )
            # If the base measure is already a direct measure, it's already in the columns
        else:
            base_columns.append(col_node)
    return Select(
        columns=base_columns,
        from_=ast.from_,
        joins=ast.joins,
        where=ast.where,
        group_by=ast.group_by,
        having=ast.having,
        grouping=ast.grouping,
    )


def _window_for(
    m: ResolvedMeasure,
    resolved: ResolvedQuery,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    assert m.cumulative_time_dimension is not None
    return _build_cumulative_window(
        m,
        m.cumulative_time_dimension,
        dialect,
        _group_dimensions(resolved, m.cumulative_time_dimension),
        model,
    )


def _lookback_windows(
    ast: Select,
    resolved: ResolvedQuery,
    cumulative_measures: list[ResolvedMeasure],
    model: SemanticModel | None,
    dialect: Dialect | None,
    base_name: str,
    over_cte: bool,
) -> list[tuple[str, list[CTE], list[ResolvedMeasure]]]:
    """One window CTE per set of time filters, when any metric has one.

    Returns ``(window CTE name, CTEs to add, metrics it computes)``; empty when
    no cumulative metric's time dimension is filtered, or the query rolls up
    (a subtotal row has no single group to look its value up by). Metrics with
    no time filter get a window CTE over ``base_name`` itself, so every
    metric is read the same way once one of them has to look back.

    ``HAVING`` stays in the look-back: it picks groups, not periods, and
    without a time filter the windows read only the groups it keeps.
    """
    if ast.grouping is not None:
        return []
    groups: list[tuple[list[Expr], list[ResolvedMeasure]]] = []
    for m in cumulative_measures:
        assert m.cumulative_time_dimension is not None
        predicates = time_filters(resolved, m.cumulative_time_dimension)
        group = next((g for g in groups if g[0] == predicates), None)
        if group is None:
            groups.append((predicates, [m]))
        else:
            group[1].append(m)
    if all(not predicates for predicates, _ in groups):
        return []

    windows: list[tuple[str, list[CTE], list[ResolvedMeasure]]] = []
    for index, (predicates, metrics) in enumerate(groups, 1):
        suffix = "" if index == 1 else f"_{index}"
        name = f"cumulative_window{suffix}"
        ctes: list[CTE] = []
        source = base_name
        lookback = (
            lookback_query(ast, predicates, f"_lookback{suffix}", resolved) if predicates else None
        )
        if lookback is not None:
            added, body = lookback
            source = f"cumulative_lookback{suffix}"
            base = _base_query(body, resolved, model, dialect, over_cte)
            ctes += [*added, CTE(name=source, query=base)]
        columns: list[Expr] = [
            AliasedExpr(expr=ColumnRef(name=d.name), alias=d.name) for d in resolved.dimensions
        ]
        columns += [
            AliasedExpr(expr=_window_for(m, resolved, model, dialect), alias=m.name)
            for m in metrics
        ]
        ctes.append(
            CTE(name=name, query=Select(columns=columns, from_=From(source=source, alias=source)))
        )
        windows.append((name, ctes, metrics))
    return windows


def _all_of(predicates: Iterable[Expr]) -> Expr | None:
    result: Expr | None = None
    for predicate in predicates:
        result = predicate if result is None else BinaryOp(left=result, op="AND", right=predicate)
    return result


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
    return cast_measure_to_resolved_type(expr, base_meas, model.settings, dialect, model)


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
