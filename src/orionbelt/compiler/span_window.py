"""Rolling windows over a span of time: the measure over its rows in the span.

A cumulative metric with ``windowSpan: {count: 90, unit: day}`` is, for each
row the query shows, its measure evaluated over the source rows whose time
falls in the 90 days ending at the end of that row's period::

    Revenue 90d (2025-03)  =  SUM(amount) over orders with
                              2025-04-01 - 90 days <= order_date < 2025-04-01

Unlike ``window: N``, which combines the per-period values the query already
computed, the span re-aggregates the rows, so any aggregation is right: a
distinct count counts a customer once across the span, an average weighs every
row, and ``first`` / ``last`` read the earliest or latest row in it.

The rows come from the multi-fact planner's legs (``cfl.py``), which project
each measure's raw values and leave the aggregation to an outer query that
knows how to rebuild it (``COUNT(DISTINCT ...)``, an ordered aggregate's sort
key). Here that outer query reads the rows joined to the shown periods by the
span instead of grouped by their own day::

    span_keys    -- the periods and other dimensions the query shows
    span_rows    -- the legs, at day grain, without the time filter
    SELECT k.period, k.<dims>, AGG(r.value)
    FROM span_keys k LEFT JOIN span_rows r
      ON r.time >= end(k.period) - span AND r.time < end(k.period)
     AND r.<dims> = k.<dims>                       -- NULL-safe
    GROUP BY k.period, k.<dims>

The time filter picks the periods shown; it does not clip what a window
reads, so March's 90 days still reach into December when only Q1 is shown.
Every other filter applies to the rows.
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
    FunctionCall,
    IsNull,
    Join,
    JoinType,
    Literal,
    Select,
)
from orionbelt.compiler.grain_dedup import detect_dedup_measures
from orionbelt.compiler.resolution import (
    QueryResolver,
    ResolutionError,
    ResolvedMeasure,
    ResolvedQuery,
)
from orionbelt.compiler.time_lookback import (
    lookback_query,
    null_safe_eq,
    rename_cte_sources,
    time_filters,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import DataType, TimeGrain

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import DataObject, SemanticModel

#: Grains finer than a day: a span or period in these needs rows at that grain.
_FINER_THAN_A_DAY = (TimeGrain.HOUR, TimeGrain.MINUTE, TimeGrain.SECOND)
_FINENESS = [
    TimeGrain.SECOND,
    TimeGrain.MINUTE,
    TimeGrain.HOUR,
    TimeGrain.DAY,
]


def _refuse(message: str, name: str) -> ResolutionError:
    return ResolutionError(
        [
            SemanticError(
                code="WINDOW_SPAN_NOT_SUPPORTED",
                message=message,
                path=f"metrics.{name}.windowSpan",
            )
        ]
    )


def _row_grain(period: TimeGrain, unit: TimeGrain) -> TimeGrain:
    """The grain the rows are read at: a day, or finer where the period or the
    span's unit is. A window's ends fall on period and unit boundaries, so rows
    truncated to that grain land on the same side of them as the rows do."""
    finer = [g for g in (period, unit) if g in _FINER_THAN_A_DAY]
    return min(finer, key=_FINENESS.index) if finer else TimeGrain.DAY


def span_value_ctes(
    resolved: ResolvedQuery,
    measures: list[ResolvedMeasure],
    *,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    query: QueryObject,
    base_name: str,
) -> list[tuple[str, list[CTE], list[ResolvedMeasure]]]:
    """One value CTE per span metric: its value for each shown group.

    Returns ``(value CTE name, CTEs to add, [metric])`` in the shape the
    cumulative wrapper joins onto the shown rows.
    """
    values: list[tuple[str, list[CTE], list[ResolvedMeasure]]] = []
    for index, m in enumerate(measures, 1):
        values.append(
            _span_value(
                resolved,
                m,
                index,
                model=model,
                dialect=dialect,
                qualify_table=qualify_table,
                query=query,
                base_name=base_name,
            )
        )
    return values


def _span_value(
    resolved: ResolvedQuery,
    m: ResolvedMeasure,
    index: int,
    *,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    query: QueryObject,
    base_name: str,
) -> tuple[str, list[CTE], list[ResolvedMeasure]]:
    from orionbelt.compiler.cfl import CFLPlanner
    from orionbelt.compiler.cumulative_wrap import _group_dimensions

    span = m.cumulative_window_span
    assert span is not None and m.cumulative_time_dimension is not None
    assert m.cumulative_measure is not None
    time_name = m.cumulative_time_dimension
    time_dim = next(d for d in resolved.dimensions if d.name == time_name)
    period = time_dim.grain or model.dimensions[time_dim.dimension].time_grain
    if period is None:
        if model.dimensions[time_dim.dimension].result_type is not DataType.DATE:
            raise _refuse(
                f"Cumulative metric '{m.name}' has a windowSpan over '{time_name}', which "
                "has no time grain to end each window at; select it at a grain "
                f"('{time_name}:day') or declare its timeGrain",
                m.name,
            )
        period = TimeGrain.DAY
    if resolved.coalesce_aliases:
        raise _refuse(
            f"Cumulative metric '{m.name}' with a windowSpan cannot be selected beside a "
            "coalesce dimension",
            m.name,
        )

    # The rows: the query's groups and the measure, with the time dimension at
    # the grain the windows' ends fall on. A dimension over the same date
    # column (``Order Date:year``) is a position on the time axis, not a group:
    # January's 90 days still reach into December.
    row_grain = _row_grain(period, span.unit)
    groups = set(_group_dimensions(resolved, time_name))
    dimensions: list[str] = [
        d.dimension if d.grain is None else f"{d.dimension}:{d.grain.value}"
        for d in resolved.dimensions
        if d.name in groups
    ]
    rows_query = QueryObject(
        select=QuerySelect(
            dimensions=[*dimensions, f"{time_dim.dimension}:{row_grain.value}"],
            measures=[m.cumulative_measure],
        ),
        where=list(query.where),
        use_path_names=list(query.use_path_names),
        allow_fan_out=query.allow_fan_out,
    )
    rows = QueryResolver().resolve(rows_query, model, qualify_table=qualify_table)
    if detect_dedup_measures(rows, model).measures:
        raise _refuse(
            f"Cumulative metric '{m.name}' has a windowSpan over '{m.cumulative_measure}', "
            "which is deduplicated per row of a join; a window over its rows would count "
            "them once per day rather than once per span",
            m.name,
        )
    planner = CFLPlanner()
    by_object, cross_fact = planner._group_measures_by_object(rows, model)
    plan = planner._plan_union_all(
        rows,
        model,
        by_object,
        cross_fact,
        qualify_table=qualify_table,
        union_by_name=dialect.capabilities.supports_union_all_by_name,
        dialect=dialect,
    )
    ast = plan.ast
    # A window reads past the time filter; the shown periods are the base's.
    row_time = next(d.name for d in rows.dimensions if d.dimension == time_dim.dimension)
    lookback = lookback_query(ast, time_filters(rows, row_time), "_lookback", rows)
    if lookback is not None:
        added, body = lookback
        ast = Select(
            columns=body.columns,
            from_=body.from_,
            joins=body.joins,
            group_by=body.group_by,
            ctes=[*ast.ctes, *added],
        )

    # The rows' CTEs, renamed so they sit beside the query's own.
    prefix = f"span_{index}_"
    renamed = {cte.name: prefix + cte.name for cte in ast.ctes}
    row_ctes = [
        CTE(name=renamed[cte.name], query=rename_cte_sources(cte.query, renamed))
        for cte in ast.ctes
    ]
    rows_source = ast.from_
    assert rows_source is not None and isinstance(rows_source.source, str)
    rows_alias = rows_source.alias or rows_source.source

    keys_name = f"span_keys_{index}"
    group_names = [d.name for d in resolved.dimensions]
    keys = CTE(
        name=keys_name,
        query=Select(
            columns=[AliasedExpr(expr=ColumnRef(name=n), alias=n) for n in group_names],
            from_=From(source=base_name, alias=base_name),
            distinct=True,
        ),
    )

    def key(name: str) -> ColumnRef:
        return ColumnRef(table=keys_name, name=name)

    def row(name: str) -> ColumnRef:
        return ColumnRef(table=rows_alias, name=name)

    end = FunctionCall(
        name="date_add", args=[Literal.string(period.value), Literal.number(1), key(time_name)]
    )
    start = FunctionCall(
        name="date_add", args=[Literal.string(span.unit.value), Literal.number(-span.count), end]
    )
    on: Expr = BinaryOp(
        left=BinaryOp(left=row(row_time), op=">=", right=start),
        op="AND",
        right=BinaryOp(left=row(row_time), op="<", right=end),
    )
    others = [d.name for d in rows.dimensions if d.name != row_time]
    for other in others:
        on = BinaryOp(left=on, op="AND", right=null_safe_eq(key(other), row(other)))
    if not others and not dialect.capabilities.supports_range_only_outer_join:
        # True whenever the range holds: both times are then non-NULL.
        on = BinaryOp(
            left=on,
            op="AND",
            right=BinaryOp(
                left=IsNull(expr=key(time_name), negated=True),
                op="=",
                right=IsNull(expr=row(row_time), negated=True),
            ),
        )

    aggregate = next(
        c.expr
        for c in ast.columns
        if isinstance(c, AliasedExpr) and c.alias == m.cumulative_measure
    )
    name = f"span_value_{index}"
    value = CTE(
        name=name,
        query=Select(
            columns=[
                *(AliasedExpr(expr=key(n), alias=n) for n in group_names),
                AliasedExpr(expr=aggregate, alias=m.name),
            ],
            from_=From(source=keys_name, alias=keys_name),
            joins=[
                Join(
                    join_type=JoinType.LEFT,
                    source=renamed[rows_source.source],
                    alias=rows_alias,
                    on=on,
                )
            ],
            group_by=[key(n) for n in group_names],
        ),
    )
    return name, [*row_ctes, keys, value], [m]
