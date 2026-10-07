"""Read a query's rows past its own time filter.

A time filter on a cumulative or period-over-period query says which periods to
*show*. It does not say which periods the values may *read*: year-to-date for
March reads January and February, and a month-over-month change for March reads
February, whether or not the query shows them. Applied to the source rows, as
every other filter is, the filter cut that history away - year-to-date started
at the first month shown, and the first month's change was NULL.

So the wrappers compute those values over a *look-back* copy of the query: the
same plan with the time filters taken out of every ``WHERE`` it contains, and
everything else - the joins, the other filters, the multi-fact legs - as it
was. The shown rows and the plain measures still come from the query as asked.
"""

from __future__ import annotations

from dataclasses import replace

from orionbelt.ast.nodes import (
    CTE,
    BinaryOp,
    Except,
    Expr,
    IsNull,
    Join,
    RawSQL,
    Select,
    UnionAll,
    Unnest,
)
from orionbelt.compiler.resolution import ResolvedQuery

CTEQuery = Select | UnionAll | Except | RawSQL


def time_filters(resolved: ResolvedQuery, time_dim_name: str) -> list[Expr]:
    """The query's ``where`` predicates on the time dimension's own column.

    "Own column" covers a filter on the dimension itself and on any dimension
    or qualified column over the same date column at another grain: ``Order
    Year = 2022`` limits the months shown exactly as ``Order Month >= ...``
    does. A filter on anything else - another dimension, a static model
    filter - limits the data, and stays.
    """
    time_dim = next((d for d in resolved.dimensions if d.name == time_dim_name), None)
    if time_dim is None or time_dim.via is not None:
        return []
    axis = (time_dim.object_name, time_dim.column_name)
    return [f.expression for f in resolved.where_filters if f.subject == axis]


def null_safe_eq(left: Expr, right: Expr) -> Expr:
    """``left = right`` that also matches when both sides are NULL.

    Spelled out rather than ``IS NOT DISTINCT FROM``, which not every engine
    accepts, so a NULL dimension value still finds its look-back row.
    """
    return BinaryOp(
        left=BinaryOp(left, "=", right),
        op="OR",
        right=BinaryOp(IsNull(expr=left), "AND", IsNull(expr=right)),
    )


def lookback_query(
    ast: Select, predicates: list[Expr], suffix: str
) -> tuple[list[CTE], Select] | None:
    """*ast* with *predicates* removed from every ``WHERE``, ready to sit beside it.

    Returns the CTEs the copy needs in addition to ``ast.ctes`` and the copy's
    body, which reads them. A CTE the removal changes is added under its name
    plus *suffix* (and so is every CTE that reads a renamed one), because both versions
    end up in one statement. Returns ``None`` when no predicate was found, so
    the caller keeps the query as it is.
    """
    renamed: dict[str, str] = {}
    added: list[CTE] = []
    for cte in ast.ctes:
        query = _rename_query(_strip_query(cte.query, predicates), renamed)
        if query != cte.query:
            renamed[cte.name] = cte.name + suffix
            added.append(CTE(name=renamed[cte.name], query=query))
    own = replace(ast, ctes=[])
    body = _rename_select(_strip_select(own, predicates), renamed)
    if not added and body == own:
        return None
    return added, body


def _strip_query(query: CTEQuery, predicates: list[Expr]) -> CTEQuery:
    match query:
        case Select():
            return _strip_select(query, predicates)
        case UnionAll():
            return replace(query, queries=[_strip_select(q, predicates) for q in query.queries])
        case Except():
            return replace(
                query,
                left=_strip_select(query.left, predicates),
                right=_strip_select(query.right, predicates),
            )
    return query


def _strip_select(select: Select, predicates: list[Expr]) -> Select:
    """*select* with every conjunct equal to one of *predicates* removed."""
    source = select.from_
    if source is not None and isinstance(source.source, Select):
        source = replace(source, source=_strip_select(source.source, predicates))
    joins = [
        replace(j, source=_strip_select(j.source, predicates))
        if isinstance(j, Join) and isinstance(j.source, Select)
        else j
        for j in select.joins
    ]
    return replace(
        select,
        from_=source,
        joins=joins,
        where=_without(select.where, predicates),
        ctes=[replace(c, query=_strip_query(c.query, predicates)) for c in select.ctes],
    )


def _without(where: Expr | None, predicates: list[Expr]) -> Expr | None:
    """*where* minus the ``AND``-ed conjuncts equal to one of *predicates*."""
    if where is None:
        return None
    result: Expr | None = None
    for conjunct in _conjuncts(where):
        if conjunct in predicates:
            continue
        result = conjunct if result is None else BinaryOp(left=result, op="AND", right=conjunct)
    return result


def _conjuncts(expr: Expr) -> list[Expr]:
    if isinstance(expr, BinaryOp) and expr.op.upper() == "AND":
        return [*_conjuncts(expr.left), *_conjuncts(expr.right)]
    return [expr]


def _rename_query(query: CTEQuery, renamed: dict[str, str]) -> CTEQuery:
    match query:
        case Select():
            return _rename_select(query, renamed)
        case UnionAll():
            return replace(query, queries=[_rename_select(q, renamed) for q in query.queries])
        case Except():
            return replace(
                query,
                left=_rename_select(query.left, renamed),
                right=_rename_select(query.right, renamed),
            )
    return query


def _rename_select(select: Select, renamed: dict[str, str]) -> Select:
    """*select* reading each renamed CTE under its new name.

    Only the source changes: the alias stays, so the columns that name it
    still resolve.
    """
    if not renamed:
        return select
    from_ = select.from_
    if from_ is not None:
        source, alias = _renamed_source(from_.source, from_.alias, renamed)
        from_ = replace(from_, source=source, alias=alias)
    joins: list[Join | Unnest] = []
    for join in select.joins:
        if isinstance(join, Join):
            source, alias = _renamed_source(join.source, join.alias, renamed)
            join = replace(join, source=source, alias=alias)
        joins.append(join)
    return replace(select, from_=from_, joins=joins)


def _renamed_source(
    source: str | Select, alias: str | None, renamed: dict[str, str]
) -> tuple[str | Select, str | None]:
    """A FROM / JOIN source and alias, reading a renamed CTE by its new name."""
    if isinstance(source, Select):
        return _rename_select(source, renamed), alias
    if source in renamed:
        return renamed[source], alias or source
    return source, alias
