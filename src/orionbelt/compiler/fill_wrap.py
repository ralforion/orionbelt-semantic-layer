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
from orionbelt.compiler.expr_rewrite import collect_referenced_tables
from orionbelt.compiler.filter_resolution import conjuncts
from orionbelt.compiler.outer_order_by import remap_order_by
from orionbelt.compiler.resolution import (
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    make_dimension_expr,
)
from orionbelt.compiler.time_lookback import null_safe_eq, time_filters
from orionbelt.dialect.base import Dialect
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.types import parse_data_type

_SOURCE = "fill_source"
_RANGE = "fill_range"
_SPINE = "fill_spine"
_MEMBERS = "fill_members"
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
    lower, upper = _bounds(resolved, time, model, dialect)
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
            elif empty_values:
                value = _empty_value(measures.get(alias), ColumnRef(name=alias, table=_SOURCE))
            else:
                value = ColumnRef(name=alias, table=_SOURCE)
            columns.append(AliasedExpr(expr=value, alias=alias))
        return columns

    periods = Select(
        columns=projection(
            as_shown(ColumnRef(name="spine_date", table=_SPINE)),
            _MEMBERS if others else _SOURCE,
            True,
        ),
        from_=From(source=_SPINE, alias=_SPINE),
        joins=list(joins),
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
) -> tuple[Expr | None, Expr | None]:
    """The first and last period the where filters on *time* admit.

    Each bound is read as a value of the time column and projected the way the
    dimension is (grain and cast), so it compares with the query's periods.
    Only a single constant bound on each side is read, at the top level or in
    an ``and`` group; anything else (two lower bounds, a relative range, an
    equality) leaves that side to the data.
    """
    assert time.grain is not None
    grain = time.grain
    declared = model.data_objects[time.object_name].columns[time.column_name].abstract_type
    column_type = declared.value
    if column_type not in ("date", "timestamp"):
        return None, None
    lowers: list[Expr] = []
    uppers: list[Expr] = []
    for filtered in time_filters(resolved, time.name):
        for predicate in conjuncts(filtered):
            if isinstance(predicate, Between) and not predicate.negated:
                lowers.append(predicate.low)
                uppers.append(predicate.high)
            elif isinstance(predicate, BinaryOp) and predicate.op in (">", ">="):
                lowers.append(predicate)
            elif isinstance(predicate, BinaryOp) and predicate.op in ("<", "<="):
                uppers.append(predicate)

    def at(value: Expr) -> Expr:
        return make_dimension_expr(model, time, dialect, column=value)

    def day_after(value: Expr, days: int) -> Expr:
        # RawSQL: the dialect's date arithmetic, over a constant bound.
        return RawSQL(sql=dialect.date_add_sql(dialect.compile_expr(value), "day", days))

    def period(values: list[Expr]) -> Expr | None:
        if len(values) != 1:
            return None
        bound, op = values[0], None
        if isinstance(bound, BinaryOp):
            op = bound.op
            bound = bound.right
        if not _constant(bound):
            return None
        typed: Expr = Cast(expr=bound, type_name=column_type)
        if op == ">" and column_type == "date":
            # ``> Nov 30`` starts in December.
            return at(day_after(typed, 1))
        if op == "<" and column_type == "date":
            # ``< May 1`` ends in April, ``< May 15`` in May.
            return at(day_after(typed, -1))
        if op == "<":
            # ``< Apr 1 12:00`` admits April's first hours, ``< Apr 1 00:00``
            # none of them: the period is the bound's own unless the bound
            # starts it. (``> ts`` always admits the rest of ts's period.)
            start = Cast(expr=dialect.render_time_grain(typed, grain), type_name=column_type)
            return CaseExpr(
                when_clauses=[(BinaryOp(start, "=", typed), at(day_after(typed, -1)))],
                else_clause=at(typed),
            )
        return at(typed)

    return period(lowers), period(uppers)


def _constant(value: Expr) -> bool:
    tables: set[str] = set()
    collect_referenced_tables(value, tables)
    return not tables


__all__ = ["wrap_with_fill"]
