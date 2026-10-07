"""Wrapper CTE for cumulative (running/rolling/grain-to-date) metrics.

Cumulative metrics aggregate already-aggregated measures along a time
dimension. Three core patterns:

| Pattern        | SQL                                                          |
|----------------|--------------------------------------------------------------|
| Running total  | ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW             |
| Rolling window | self-join on ``date_diff(grain, prior, current) <= N - 1``   |
| Grain-to-date  | PARTITION BY TRUNC(grain) + ROWS UNBOUNDED PRECEDING         |

A rolling window counts calendar periods, so it cannot be a ``ROWS`` frame (a
gap would make it reach too far back), and Dremio rejects ``RANGE`` frames with
an offset; see :func:`_rolling_query`.

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
    CaseExpr,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    IsNull,
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
        # Rolling window over rows: only under ROLLUP / CUBE, where there is
        # no single group to join a calendar window back to. Everywhere else
        # ``_rolling_query`` counts calendar periods instead.
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
    value_ctes = _value_ctes(ast, resolved, cumulative_measures, model, dialect, cte_name, over_cte)
    order_by = outer_order_by(resolved, model)

    if not value_ctes:
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

    # A time filter selects the rows shown; the windows read past it, and a
    # rolling window reads calendar periods rather than rows. Each value CTE
    # carries one row per group with the metric's value, and the rows the
    # query asked for pick theirs up by their dimensions.
    joined_name = "cumulative_joined"
    joined_columns: list[Expr] = [
        AliasedExpr(expr=ColumnRef(table=cte_name, name=d.name), alias=d.name)
        for d in resolved.dimensions
    ]
    window_of = {m.name: name for name, _, metrics in value_ctes for m in metrics}
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
        for name, _, _ in value_ctes
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
            *(cte for _, ctes, _ in value_ctes for cte in ctes),
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


def _value_ctes(
    ast: Select,
    resolved: ResolvedQuery,
    cumulative_measures: list[ResolvedMeasure],
    model: SemanticModel | None,
    dialect: Dialect | None,
    base_name: str,
    over_cte: bool,
) -> list[tuple[str, list[CTE], list[ResolvedMeasure]]]:
    """The CTEs that compute the cumulative metrics apart from the shown rows.

    Returns ``(value CTE name, CTEs to add, metrics it computes)``. Needed
    when a metric's time dimension is filtered, so the values read past the
    filter, or when a metric is a rolling window, which reads calendar periods
    (:func:`_rolling_query`). Empty otherwise, and when the query rolls up: a
    subtotal row has no single group to look its value up by, so there every
    metric stays a window over the shown rows.

    Metrics are grouped by their set of time filters, and each group reads
    one source: the look-back copy of the query without those filters, or
    ``base_name`` itself. Within a group the running and grain-to-date metrics
    share one window CTE, and the rolling ones one CTE per time dimension and
    partition.

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
    if all(not predicates for predicates, _ in groups) and not any(
        _is_rolling(m) for m in cumulative_measures
    ):
        return []

    values: list[tuple[str, list[CTE], list[ResolvedMeasure]]] = []
    rolling_count = 0
    for index, (predicates, metrics) in enumerate(groups, 1):
        suffix = "" if index == 1 else f"_{index}"
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

        windowed = [m for m in metrics if not _is_rolling(m)]
        if windowed:
            name = f"cumulative_window{suffix}"
            columns: list[Expr] = [
                AliasedExpr(expr=ColumnRef(name=d.name), alias=d.name) for d in resolved.dimensions
            ]
            columns += [
                AliasedExpr(expr=_window_for(m, resolved, model, dialect), alias=m.name)
                for m in windowed
            ]
            ctes.append(
                CTE(
                    name=name,
                    query=Select(columns=columns, from_=From(source=source, alias=source)),
                )
            )
            values.append((name, ctes, windowed))
            ctes = []

        rolling: dict[tuple[str, tuple[str, ...]], list[ResolvedMeasure]] = {}
        for m in metrics:
            if _is_rolling(m):
                assert m.cumulative_time_dimension is not None
                key = (m.cumulative_time_dimension, tuple(_partition_names(m, resolved)))
                rolling.setdefault(key, []).append(m)
        for (time_dim_name, partitions), same_axis in rolling.items():
            rolling_count += 1
            name = "cumulative_rolling" + ("" if rolling_count == 1 else f"_{rolling_count}")
            query = _rolling_query(source, same_axis, time_dim_name, list(partitions), resolved)
            ctes.append(CTE(name=name, query=query))
            values.append((name, ctes, same_axis))
            ctes = []
    return values


def _is_rolling(m: ResolvedMeasure) -> bool:
    """A rolling window: ``window: N``, and no grain-to-date taking precedence."""
    return m.cumulative_window is not None and m.cumulative_grain_to_date is None


def _partition_names(m: ResolvedMeasure, resolved: ResolvedQuery) -> list[str]:
    """The dimensions a cumulative metric accumulates within, as in the window."""
    assert m.cumulative_time_dimension is not None
    groups = _group_dimensions(resolved, m.cumulative_time_dimension)
    return list(dict.fromkeys([*groups, *m.cumulative_partition_by]))


def _rolling_query(
    source: str,
    metrics: list[ResolvedMeasure],
    time_dim_name: str,
    partitions: list[str],
    resolved: ResolvedQuery,
) -> Select:
    """Rolling windows of *metrics* over *source*, counting calendar periods.

    A window over the rows counted rows: ``window: 3`` over months with a gap
    reached back past the gap, to a month outside the three. Each row here
    instead reads the rows of its own group whose period lies at most N-1
    periods of the time dimension's grain before its own, which a self-join on
    ``date_diff`` states on every engine (Dremio rejects ``RANGE`` frames with
    an offset). A period without data contributes nothing: the aggregate runs
    over the periods in the window that have a row, so ``avg`` is the average
    of those periods.

    The metrics share one join, as wide as the widest window; a narrower one
    takes only the periods its own window reaches. A time dimension without a
    grain counts days. A row without a date has no periods before it: it reads
    its own value, as a ``RANGE`` frame's NULL peers would, rather than leaving
    the join.
    """
    time_dim = next(d for d in resolved.dimensions if d.name == time_dim_name)
    unit = time_dim.grain.value if time_dim.grain is not None else "day"
    current, prior = "cumulative_current", "cumulative_prior"
    periods_back = FunctionCall(
        name="date_diff",
        args=[
            Literal.string(unit),
            ColumnRef(table=prior, name=time_dim_name),
            ColumnRef(table=current, name=time_dim_name),
        ],
    )
    undated = IsNull(expr=ColumnRef(table=prior, name=time_dim_name))
    widest = max(m.cumulative_window or 1 for m in metrics)
    within = BinaryOp(
        left=BinaryOp(left=periods_back, op=">=", right=Literal.number(0)),
        op="AND",
        right=BinaryOp(left=periods_back, op="<=", right=Literal.number(widest - 1)),
    )
    both_undated = BinaryOp(
        left=IsNull(expr=ColumnRef(table=current, name=time_dim_name)), op="AND", right=undated
    )
    on = _all_of(
        [
            *(
                null_safe_eq(ColumnRef(table=current, name=p), ColumnRef(table=prior, name=p))
                for p in partitions
            ),
            BinaryOp(left=within, op="OR", right=both_undated),
        ]
    )
    columns: list[Expr] = [
        AliasedExpr(expr=ColumnRef(table=current, name=d.name), alias=d.name)
        for d in resolved.dimensions
    ]
    for m in metrics:
        span = m.cumulative_window or 1
        value: Expr = ColumnRef(table=prior, name=m.cumulative_measure or m.name)
        if span < widest:
            reaches = BinaryOp(
                left=BinaryOp(left=periods_back, op="<=", right=Literal.number(span - 1)),
                op="OR",
                right=undated,
            )
            value = CaseExpr(when_clauses=[(reaches, value)])
        aggregate = FunctionCall(name=_CUMULATIVE_AGG_MAP[m.cumulative_type], args=[value])
        columns.append(AliasedExpr(expr=aggregate, alias=m.name))
    return Select(
        columns=columns,
        from_=From(source=source, alias=current),
        joins=[Join(join_type=JoinType.INNER, source=source, alias=prior, on=on)],
        group_by=[ColumnRef(table=current, name=d.name) for d in resolved.dimensions],
    )


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
