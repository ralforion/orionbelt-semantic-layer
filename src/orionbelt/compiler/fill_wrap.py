"""Return every period of the query's time dimension (``fill: true``).

The finished query is wrapped, and its rows are joined onto a date spine:

=================  ==========================================================
CTE                Purpose
=================  ==========================================================
``fill_source``    The query as planned, without its ORDER BY and LIMIT
``fill_range``     First and last period: the where bounds, else the data's
``fill_spine``     One row per period, from the dialect's date spine
``fill_members``   The other dimensions' tuples the query returns
``filled``         Spine x members, the query's row where it has one
=================  ==========================================================

An empty period has no row to read, so its values are what the query computes
over no rows: a count is 0, a measure with a ``defaultValue`` reads it, and
everything else is NULL. A row whose time is NULL (an order without a date) is
no period, and is kept as it is rather than dropped by the join.

A cumulative metric is the exception: its window reads other periods, so an
empty period shows the value its row would have shown, over the periods with
rows in its window (``fill_cumulative``, see :func:`_cumulative_ctes`). A
running total carries on, a rolling window drops the periods it no longer
reaches. The empty period's own 0 takes no part, so filling never changes the
value of a period with rows.

The range never cuts the data off: a bound is only taken where it lies outside
the data, so a period with rows is always on the spine.
"""

from __future__ import annotations

from dataclasses import replace

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    Between,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    IsNull,
    Join,
    JoinType,
    Literal,
    RawSQL,
    Select,
    SubqueryExpr,
    UnionAll,
)
from orionbelt.compiler.cumulative_wrap import (
    _apply_metric_cast,
    _is_rolling,
    _partition_names,
    _rolling_query,
    _window_for,
)
from orionbelt.compiler.expr_rewrite import collect_referenced_tables, map_nodes
from orionbelt.compiler.filter_resolution import conjuncts
from orionbelt.compiler.outer_order_by import remap_order_by
from orionbelt.compiler.resolution import (
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
    make_dimension_expr,
    make_period_expr,
)
from orionbelt.compiler.time_lookback import null_safe_eq
from orionbelt.dialect.base import Dialect
from orionbelt.models.semantic import CumulativeAggType, SemanticModel
from orionbelt.models.types import parse_data_type

_SOURCE = "fill_source"
_RANGE = "fill_range"
_SPINE = "fill_spine"
_MEMBERS = "fill_members"
#: The source's time on a row of the cumulative source union, NULL on an added one.
_DATED = "fill_dated"
_FILLED = "filled"

_COUNTS = frozenset({"count", "count_distinct"})


def wrap_with_fill(
    ast: Select, resolved: ResolvedQuery, model: SemanticModel, dialect: Dialect
) -> Select:
    """Join *ast*'s rows onto every period of the time dimension."""
    time = next(d for d in resolved.dimensions if d.grain is not None)
    assert time.grain is not None  # resolution refuses fill without one
    # A coalesce group is one column of the result, under its alias.
    others = list(dict.fromkeys(d.coalesce_alias or d.name for d in resolved.dimensions))
    others.remove(time.name)
    measures = {m.name: m for m in resolved.measures}
    aliases = [_alias(column) for column in ast.columns]

    source = replace(ast, ctes=[], order_by=[], limit=None, offset=None)
    lower, upper, on_periods = _bounds(resolved, time, model, dialect)
    period = ColumnRef(name=time.name, table=_SOURCE)
    data_min = FunctionCall(name="MIN", args=[ColumnRef(name=time.name)])
    data_max = FunctionCall(name="MAX", args=[ColumnRef(name=time.name)])
    # The spine is generated over dates (BigQuery's takes nothing else), and
    # each period is cast back to the dimension's type where it is shown. A
    # period of a day or longer starts at midnight, so nothing is lost.
    date_range = Select(
        columns=[
            AliasedExpr(
                expr=Cast(expr=_outer(lower, data_min, "<"), type_name="date"), alias="min_date"
            ),
            AliasedExpr(
                expr=Cast(expr=_outer(upper, data_max, ">"), type_name="date"), alias="max_date"
            ),
        ],
        from_=From(source=_SOURCE, alias=_SOURCE),
    )
    range_ref = dialect.quote_identifier(_RANGE)
    # RawSQL: the dialect's date-spine generator (generate_series, a recursive
    # CTE, GENERATE_DATE_ARRAY ...), shared with period-over-period. Not
    # expressible as a typed Select.
    spine_sql = dialect.render_date_spine_cte_sql(
        min_date=f"(SELECT {dialect.quote_identifier('min_date')} FROM {range_ref})",
        max_date=f"(SELECT {dialect.quote_identifier('max_date')} FROM {range_ref})",
        grain=time.grain.value,
        offset=-1,
        offset_grain=time.grain.value,
    )
    # The generator names its column bare, which Snowflake folds to upper
    # case; it is re-exposed under the quoted name the typed nodes use.
    spine = RawSQL(
        sql=f"SELECT spine_date AS {dialect.quote_identifier('spine_date')}\n"
        f"FROM (\n{spine_sql}\n) AS {dialect.quote_identifier('spine')}"
    )
    ctes = [
        *ast.ctes,
        CTE(name=_SOURCE, query=source),
        CTE(name=_RANGE, query=date_range),
        CTE(name=_SPINE, query=spine),
    ]

    joins: list[Join] = []
    on: Expr = BinaryOp(
        left=Cast(expr=period, type_name="date"),
        op="=",
        right=ColumnRef(name="spine_date", table=_SPINE),
    )
    if others:
        ctes.append(
            CTE(
                name=_MEMBERS,
                query=Select(
                    columns=[ColumnRef(name=name) for name in others],
                    from_=From(source=_SOURCE, alias=_SOURCE),
                    distinct=True,
                ),
            )
        )
        joins.append(Join(join_type=JoinType.CROSS, source=_MEMBERS, alias=_MEMBERS))
        for name in others:
            on = BinaryOp(
                left=on,
                op="AND",
                right=null_safe_eq(
                    ColumnRef(name=name, table=_SOURCE), ColumnRef(name=name, table=_MEMBERS)
                ),
            )
    joins.append(Join(join_type=JoinType.LEFT, source=_SOURCE, alias=_SOURCE, on=on))
    cumulative_ctes, cumulative_values, reach = _cumulative_ctes(
        resolved, time.name, others, model, dialect
    )
    ctes += cumulative_ctes
    spine_period = ColumnRef(name="spine_date", table=_SPINE)
    for value_cte in dict.fromkeys(cumulative_values.values()):
        joins.append(
            Join(
                join_type=JoinType.LEFT,
                source=value_cte,
                alias=value_cte,
                on=_all_of(
                    [
                        BinaryOp(
                            left=Cast(
                                expr=ColumnRef(name=time.name, table=value_cte), type_name="date"
                            ),
                            op="=",
                            right=spine_period,
                        ),
                        *(
                            null_safe_eq(
                                ColumnRef(name=d.name, table=value_cte),
                                ColumnRef(name=d.coalesce_alias or d.name, table=_MEMBERS),
                            )
                            for d in resolved.dimensions
                            if d.name != time.name
                        ),
                    ]
                ),
            )
        )

    result_type = model.dimensions[time.dimension].result_type

    def as_shown(spine_date: Expr) -> Expr:
        """A spine date as the dimension shows its periods."""
        return dialect.cast_to_obml_type(spine_date, parse_data_type(result_type.value))

    def projection(time_value: Expr, member_table: str, empty_values: bool) -> list[Expr]:
        columns: list[Expr] = []
        for alias in aliases:
            value: Expr
            if alias == time.name:
                value = time_value
            elif alias in others:
                value = ColumnRef(name=alias, table=member_table)
            elif empty_values and alias in cumulative_values:
                # The query's own value where the period has a row, so filling
                # changes none of them.
                value_cte = cumulative_values[alias]
                filled_value = _apply_metric_cast(
                    ColumnRef(name=alias, table=value_cte), alias, model, dialect
                )
                if alias in reach:
                    # A count is 0 over the added rows alone; over no period
                    # with rows it is NULL, as an ``asOf`` there answers.
                    reached = BinaryOp(
                        left=ColumnRef(name=reach[alias], table=value_cte),
                        op=">",
                        right=Literal.number(0),
                    )
                    filled_value = CaseExpr(when_clauses=[(reached, filled_value)])
                value = CaseExpr(
                    when_clauses=[(IsNull(expr=period), filled_value)],
                    else_clause=ColumnRef(name=alias, table=_SOURCE),
                )
            elif empty_values:
                value = _empty_value(measures.get(alias), ColumnRef(name=alias, table=_SOURCE))
            else:
                value = ColumnRef(name=alias, table=_SOURCE)
            columns.append(AliasedExpr(expr=value, alias=alias))
        return columns

    shown = as_shown(ColumnRef(name="spine_date", table=_SPINE))
    # A filter on the dimension's periods, applied to the spine's: the same
    # comparison the where makes of each row's period. A row the where kept
    # passes it, so no period with data is dropped.
    spine_filter: Expr | None = None
    for predicate in on_periods:
        on_spine = _on_spine(predicate, shown)
        spine_filter = on_spine if spine_filter is None else BinaryOp(spine_filter, "AND", on_spine)
    periods = Select(
        columns=projection(shown, _MEMBERS if others else _SOURCE, True),
        from_=From(source=_SPINE, alias=_SPINE),
        joins=list(joins),
        where=spine_filter,
    )
    # The rows without a period, typed as the spine's column so the two
    # branches agree on every engine.
    no_period = Select(
        columns=projection(
            as_shown(
                SubqueryExpr(
                    query=Select(
                        columns=[ColumnRef(name="spine_date")],
                        from_=From(source=_SPINE, alias=_SPINE),
                        where=BinaryOp(left=Literal.number(1), op="=", right=Literal.number(0)),
                    )
                )
            ),
            _SOURCE,
            False,
        ),
        from_=From(source=_SOURCE, alias=_SOURCE),
        where=IsNull(expr=period),
    )
    ctes.append(CTE(name=_FILLED, query=UnionAll(queries=[periods, no_period])))

    return Select(
        columns=[AliasedExpr(expr=ColumnRef(name=alias), alias=alias) for alias in aliases],
        from_=From(source=_FILLED, alias=_FILLED),
        order_by=remap_order_by(ast.order_by, resolved, model),
        limit=ast.limit,
        offset=ast.offset,
        ctes=ctes,
    )


def _cumulative_ctes(
    resolved: ResolvedQuery,
    time_name: str,
    others: list[str],
    model: SemanticModel,
    dialect: Dialect,
) -> tuple[list[CTE], dict[str, str], dict[str, str]]:
    """The CTEs evaluating each cumulative metric at every spine period.

    Its windows read :attr:`ResolvedQuery.cumulative_sources`, one row per
    group and period with rows. A row per spine period and member without one
    is added, its base measure the NULL of the unmatched join, which no
    aggregate counts: the window over it reads the periods with rows, as the
    cumulative pass's own window reads them.

    ``COUNT`` is the exception, 0 rather than NULL over the added rows alone,
    so a count metric gets a companion counting the source rows its window
    reaches (:data:`_DATED`, the source's time, NULL on an added row).

    Returns the CTEs; per metric, the CTE holding its value under its name;
    and per count metric, its companion's name in that CTE.
    """
    by_source: dict[str, list[ResolvedMeasure]] = {}
    for m in resolved.measures:
        source = resolved.cumulative_sources.get(m.name)
        if source is not None:
            by_source.setdefault(source, []).append(m)
    ctes: list[CTE] = []
    values: dict[str, str] = {}
    reach: dict[str, str] = {}
    for index, (source, metrics) in enumerate(by_source.items(), 1):
        suffix = "" if index == 1 else f"_{index}"
        union_name = f"fill_cumulative_source{suffix}"
        bases = list(dict.fromkeys(m.cumulative_measure or m.name for m in metrics))
        names = [d.name for d in resolved.dimensions]
        member_of = {d.name: d.coalesce_alias or d.name for d in resolved.dimensions}
        with_rows = Select(
            columns=[
                *(ColumnRef(name=name) for name in [*names, *bases]),
                AliasedExpr(expr=ColumnRef(name=time_name), alias=_DATED),
            ],
            from_=From(source=source, alias=source),
        )
        on = _all_of(
            [
                BinaryOp(
                    left=Cast(expr=ColumnRef(name=time_name, table=source), type_name="date"),
                    op="=",
                    right=ColumnRef(name="spine_date", table=_SPINE),
                ),
                *(
                    null_safe_eq(
                        ColumnRef(name=name, table=source),
                        ColumnRef(name=member_of[name], table=_MEMBERS),
                    )
                    for name in names
                    if name != time_name
                ),
            ]
        )
        joins: list[Join] = []
        if others:
            joins.append(Join(join_type=JoinType.CROSS, source=_MEMBERS, alias=_MEMBERS))
        joins.append(Join(join_type=JoinType.LEFT, source=source, alias=source, on=on))
        shown = dialect.cast_to_obml_type(
            ColumnRef(name="spine_date", table=_SPINE),
            parse_data_type(
                model.dimensions[
                    next(d.dimension for d in resolved.dimensions if d.name == time_name)
                ].result_type.value
            ),
        )
        without_columns: list[Expr] = [
            AliasedExpr(
                expr=shown
                if name == time_name
                else ColumnRef(name=member_of[name], table=_MEMBERS),
                alias=name,
            )
            for name in names
        ]
        without_columns += [
            AliasedExpr(expr=ColumnRef(name=base, table=source), alias=base) for base in bases
        ]
        without_columns.append(
            AliasedExpr(expr=ColumnRef(name=time_name, table=source), alias=_DATED)
        )
        without_rows = Select(
            columns=without_columns,
            from_=From(source=_SPINE, alias=_SPINE),
            joins=list(joins),
            where=IsNull(expr=ColumnRef(name=time_name, table=source)),
        )
        ctes.append(CTE(name=union_name, query=UnionAll(queries=[with_rows, without_rows])))

        for m in metrics:
            if m.cumulative_type is CumulativeAggType.COUNT:
                reach[m.name] = f"fill_reach_{len(reach) + 1}"
        metrics = [
            *metrics,
            *(
                replace(m, name=reach[m.name], cumulative_measure=_DATED)
                for m in metrics
                if m.name in reach
            ),
        ]
        windowed = [m for m in metrics if not _is_rolling(m)]
        if windowed:
            name = f"fill_cumulative{suffix}"
            ctes.append(
                CTE(
                    name=name,
                    query=Select(
                        columns=[
                            *(AliasedExpr(expr=ColumnRef(name=n), alias=n) for n in names),
                            *(
                                AliasedExpr(
                                    expr=_window_for(m, resolved, model, dialect), alias=m.name
                                )
                                for m in windowed
                            ),
                        ],
                        from_=From(source=union_name, alias=union_name),
                    ),
                )
            )
            values.update((m.name, name) for m in windowed)
        rolling: dict[tuple[str, tuple[str, ...]], list[ResolvedMeasure]] = {}
        for m in metrics:
            if _is_rolling(m):
                assert m.cumulative_time_dimension is not None
                key = (m.cumulative_time_dimension, tuple(_partition_names(m, resolved)))
                rolling.setdefault(key, []).append(m)
        for count, ((axis, partitions), same_axis) in enumerate(rolling.items(), 1):
            name = f"fill_cumulative_rolling{suffix}" + ("" if count == 1 else f"_{count}")
            query = _rolling_query(union_name, same_axis, axis, list(partitions), resolved)
            ctes.append(CTE(name=name, query=query))
            values.update((m.name, name) for m in same_axis)
    return ctes, values, reach


def _all_of(predicates: list[Expr]) -> Expr:
    result = predicates[0]
    for predicate in predicates[1:]:
        result = BinaryOp(left=result, op="AND", right=predicate)
    return result


def _alias(column: Expr) -> str:
    assert isinstance(column, AliasedExpr), "every planned column carries its alias"
    return column.alias


def _empty_value(measure: ResolvedMeasure | None, value: Expr) -> Expr:
    """What *measure* reads in a period without rows: a count's 0, a declared
    default, else the NULL of no row."""
    if measure is None or measure.component_measures:
        return value
    if measure.aggregation.lower() in _COUNTS:
        return FunctionCall(name="COALESCE", args=[value, Literal.number(0)])
    return measure.with_default(value)


def _outer(bound: Expr | None, data: Expr, op: str) -> Expr:
    """The bound where it lies beyond the data, else the data's own end."""
    if bound is None:
        return data
    return CaseExpr(
        when_clauses=[
            (BinaryOp(left=IsNull(expr=data), op="OR", right=BinaryOp(bound, op, data)), bound)
        ],
        else_clause=data,
    )


def _bounds(
    resolved: ResolvedQuery, time: ResolvedDimension, model: SemanticModel, dialect: Dialect
) -> tuple[Expr | None, Expr | None, list[Expr]]:
    """The first and last period the where filters on *time* admit, and the
    filters that compare *time*'s periods, to apply to the spine.

    A filter on the dimension compares its period (``Order Month >= Jan 15``
    admits February on): its bound, truncated, only widens the range, and the
    filter itself, applied to each spine period, admits exactly what the
    ``where`` does. A filter on the date itself (a dimension without a grain, a
    qualified column) admits a period with any day it admits, and its bound is
    read as a date. Only a single constant bound on each side counts, at the top
    level or in an ``and`` group; anything else leaves that side to the data.
    """
    assert time.grain is not None
    grain = time.grain
    declared = model.data_objects[time.object_name].columns[time.column_name].abstract_type
    column_type = declared.value
    if column_type not in ("date", "timestamp") or time.via is not None:
        return None, None, []
    column = make_column_expr(model, time.object_name, time.column_name)
    dimension = model.dimensions[time.dimension]
    # Only at the grain shown: a month filter read off weekly spine dates
    # would drop a week reaching into an admitted month.
    shown_period = (
        make_period_expr(model, dimension, grain) if dimension.time_grain == grain else None
    )
    lowers: list[tuple[Expr, str]] = []
    uppers: list[tuple[Expr, str]] = []
    on_periods: list[Expr] = []
    # Conjunct by conjunct: a group that also filters another column has no
    # single subject, and its comparisons on the time column bound it all the
    # same.
    for predicate in (p for f in resolved.where_filters for p in conjuncts(f.expression)):
        subject = predicate.expr if isinstance(predicate, Between) else None
        if isinstance(predicate, BinaryOp):
            subject = predicate.left
        is_period = shown_period is not None and subject == shown_period
        if not is_period and subject != column:
            continue
        kind = "period" if is_period else "date"
        if isinstance(predicate, Between) and not predicate.negated:
            lowers.append((predicate.low, f"{kind}>="))
            uppers.append((predicate.high, f"{kind}<="))
        elif isinstance(predicate, BinaryOp) and predicate.op in (">", ">=", "<", "<="):
            (lowers if predicate.op[0] == ">" else uppers).append(
                (predicate.right, f"{kind}{predicate.op}")
            )
        else:
            continue
        if is_period:
            on_periods.append(predicate)

    def at(value: Expr) -> Expr:
        return make_dimension_expr(model, time, dialect, column=value)

    def day_after(value: Expr, days: int) -> Expr:
        # RawSQL: the dialect's date arithmetic, over a constant bound.
        return RawSQL(sql=dialect.date_add_sql(dialect.compile_expr(value), "day", days))

    def period(values: list[tuple[Expr, str]]) -> Expr | None:
        if len(values) != 1:
            return None
        bound, op = values[0]
        if not _constant(bound):
            return None
        typed: Expr = Cast(expr=bound, type_name=column_type, obml_type=True)
        if op == "date>" and column_type == "date":
            # ``> Nov 30`` starts in December.
            return at(day_after(typed, 1))
        if op == "date<" and column_type == "date":
            # ``< May 1`` ends in April, ``< May 15`` in May.
            return at(day_after(typed, -1))
        if op == "date<":
            # ``< Apr 1 12:00`` admits April's first hours, ``< Apr 1 00:00``
            # none of them: the period is the bound's own unless the bound
            # starts it. (``> ts`` always admits the rest of ts's period.)
            start = Cast(
                expr=dialect.render_time_grain(typed, grain), type_name=column_type, obml_type=True
            )
            return CaseExpr(
                when_clauses=[(BinaryOp(start, "=", typed), at(day_after(typed, -1)))],
                else_clause=at(typed),
            )
        # A period filter's bound truncated: the period holding it, which the
        # spine's own filter keeps or drops.
        return at(typed)

    return period(lowers), period(uppers), on_periods


def _on_spine(predicate: Expr, shown: Expr) -> Expr:
    """*predicate* on a period filter, comparing the spine's period *shown*."""
    operand = predicate.expr if isinstance(predicate, Between) else None
    if isinstance(predicate, BinaryOp):
        operand = predicate.left
    return map_nodes(predicate, lambda node: shown if node == operand else None)


def _constant(value: Expr) -> bool:
    tables: set[str] = set()
    collect_referenced_tables(value, tables)
    return not tables


__all__ = ["wrap_with_fill"]
