"""Sorting by a measure the query does not select.

"The three customers with the most revenue", showing only their names. The
rows are the query's own: added to its plan, ``Revenue``'s fact would choose
the base and drop the customers without orders, so a sort key would change
which rows come back. The measure is computed instead in a query of its own,
over the same dimensions and filters, joined onto the finished rows by those
dimensions, and sorted on::

    WITH ...                                    -- the query's CTEs, hoisted
    SELECT "sorted_rows"."Customer"
    FROM (<the query, unsorted>) AS "sorted_rows"
    LEFT JOIN (<Customer, Revenue>) AS "sort_keys"
      ON "sorted_rows"."Customer" = "sort_keys"."Customer" OR (both NULL)
    ORDER BY "sort_keys"."Revenue" DESC
    LIMIT 3

A row the sort query has no value for sorts as NULL. ROLLUP / CUBE is refused
at resolution: a subtotal row has no value at the detail grain.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    Select,
)
from orionbelt.compiler.star import _nulls_last
from orionbelt.compiler.time_lookback import null_safe_eq
from orionbelt.models.query import QueryObject, QuerySelect

if TYPE_CHECKING:
    from orionbelt.compiler.passes import CompileContext

SORTED_ROWS = "sorted_rows"
SORT_KEYS = "sort_keys"


def _aliases(select: Select) -> list[str] | None:
    """The output names of *select*; ``None`` where a column has none."""
    names = [col.alias for col in select.columns if isinstance(col, AliasedExpr)]
    return names if len(names) == len(select.columns) else None


def wrap_order_only(ast: Select, ctx: CompileContext) -> Select:
    """Sort the finished rows by measures computed over the query's dimensions."""
    from orionbelt.compiler.pipeline import plan_in_own_right

    query = ctx.query
    hidden = list(
        dict.fromkeys(
            ob.field for ob in query.order_by if ob.field in ctx.resolved.order_only_measures
        )
    )
    aliases = _aliases(ast)
    if aliases is None:
        return ast
    # The sort values are the ones the measures would show selected in this
    # query: beside its selected and HAVING-only measures, so the same plan
    # reads them (a filter one fact cannot reach is skipped there and said
    # so), with the same filters and join paths, a cumulative metric as of
    # the same date, and the filled periods carrying theirs.
    having_only = sorted(ctx.resolved.having_only_measures)
    measures = list(dict.fromkeys([*query.select.measures, *having_only, *hidden]))
    sort_query = QueryObject(
        select=QuerySelect(dimensions=list(query.select.dimensions), measures=measures),
        where=list(query.where),
        use_path_names=list(query.use_path_names),
        allow_fan_out=query.allow_fan_out,
        as_of=query.as_of,
        fill=query.fill,
    )
    sort_ast, sort_resolved = plan_in_own_right(
        sort_query, ctx.model, ctx.dialect, ctx.qualify_table
    )
    # Every table the sort query reads is a cache dependency of this one.
    ctx.resolved.subquery_objects.update(
        sort_resolved.required_objects | sort_resolved.subquery_objects
    )
    for step in sort_resolved.join_steps:
        ctx.resolved.subquery_objects.update((step.from_object, step.to_object))
    # What the sort query reports holds for the ranking: a filter its facts
    # could not apply leaves the order partly unfiltered.
    known = {(w.code, w.message) for w in ctx.resolved.warnings}
    for w in sort_resolved.warnings:
        if (w.code, w.message) not in known:
            ctx.resolved.warnings.append(w)
            known.add((w.code, w.message))
    sort_aliases = _aliases(sort_ast)
    if sort_aliases is None:
        return ast
    dimensions = [alias for alias in sort_aliases if alias not in measures]
    if not dimensions:
        # One row: there is nothing to sort.
        return ast

    on: Expr = null_safe_eq(
        ColumnRef(name=dimensions[0], table=SORTED_ROWS),
        ColumnRef(name=dimensions[0], table=SORT_KEYS),
    )
    for alias in dimensions[1:]:
        on = BinaryOp(
            left=on,
            op="AND",
            right=null_safe_eq(
                ColumnRef(name=alias, table=SORTED_ROWS), ColumnRef(name=alias, table=SORT_KEYS)
            ),
        )

    order_by: list[OrderByItem] = []
    for ob in query.order_by:
        expr: Expr
        if ob.field.isdigit():
            expr = Literal.number(int(ob.field))
        else:
            expr = ColumnRef(name=ob.field, table=SORT_KEYS if ob.field in hidden else SORTED_ROWS)
        order_by.append(
            OrderByItem(expr=expr, desc=ob.direction == "desc", nulls_last=_nulls_last(ob.nulls))
        )

    return Select(
        columns=[
            AliasedExpr(expr=ColumnRef(name=alias, table=SORTED_ROWS), alias=alias)
            for alias in aliases
        ],
        from_=From(
            source=replace(ast, ctes=[], order_by=[], limit=None, offset=None), alias=SORTED_ROWS
        ),
        joins=[
            Join(
                join_type=JoinType.LEFT,
                source=replace(sort_ast, order_by=[], limit=None, offset=None),
                alias=SORT_KEYS,
                on=on,
            )
        ],
        order_by=order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=ast.ctes,
    )
