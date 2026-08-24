"""How an ORDER BY reads once the planner's query has been wrapped in a CTE.

Four passes put the planner's SELECT inside a CTE and build an outer query over
it - ``total_wrap``, ``cumulative_wrap``, ``window_wrap`` and ``pop_wrap`` - and
each has to rewrite ORDER BY, because the tables the planner referenced are not
in the outer query's FROM. Out there the ordering key is a *column of the CTE*,
named by the alias the projection gave it.

A dimension arrives in two shapes and only one of them is a column reference. A
computed column's source is an expression, inlined by ``make_column_expr`` both
where the projection is built and where the ORDER BY is resolved (see
``filter_resolution.resolve_order_by_field``), so it is matched structurally
against that same construction rather than by physical column name.

Reading only the column shape is #358: the expression was inlined a second time
into the outer query, where the table it names is out of scope. The model
validated clean, ``sql_valid`` came back true, and the database rejected the
statement, naming a *logical* object so that it read as a join problem rather
than a projection-scope one. ``filter_wrap`` and ``grain_dedup``, which build
their own outer queries, already matched dimensions structurally; these four
did not, and each carried its own near-copy of the rewrite. There is one copy
now.
"""

from __future__ import annotations

from dataclasses import dataclass

from orionbelt.ast.nodes import ColumnRef, Expr, Literal, OrderByItem
from orionbelt.compiler.resolution import ResolvedQuery, make_column_expr
from orionbelt.compiler.star import _nulls_last
from orionbelt.models.semantic import SemanticModel


@dataclass(frozen=True)
class _Projection:
    """What the wrapped query projects, in the two shapes ORDER BY arrives in."""

    #: Alias by (physical column, data object), for an ordering key the planner
    #: emitted as a table-qualified reference.
    by_column: dict[tuple[str, str | None], str]

    #: Alias by the expression that produces it, in declaration order. Covers
    #: the dimensions whose source is not a column reference at all - a computed
    #: column, or one read in the query timezone - and the measures, whose
    #: aggregate the planner emits inline.
    by_expr: list[tuple[Expr, str]]


def outer_order_by(resolved: ResolvedQuery, model: SemanticModel | None) -> list[OrderByItem]:
    """The resolved ORDER BY, rewritten to name the wrapped projection."""
    projection = _projection(resolved, model)
    return [
        OrderByItem(
            expr=_outer_expr(expr, projection),
            desc=desc,
            nulls_last=_nulls_last(nulls),
        )
        for expr, desc, nulls in resolved.order_by_exprs
    ]


def remap_order_by(
    order_by: list[OrderByItem],
    resolved: ResolvedQuery,
    model: SemanticModel | None,
) -> list[OrderByItem]:
    """The same rewrite, for a wrapper that starts from the planner's own list.

    The planner has already rewritten some entries - a time-grained dimension
    reaches here as its bare alias - so this preserves what it built rather
    than resolving the query's ORDER BY a second time.
    """
    projection = _projection(resolved, model)
    return [
        OrderByItem(
            expr=_outer_expr(item.expr, projection),
            desc=item.desc,
            nulls_last=item.nulls_last,
        )
        for item in order_by
    ]


def _projection(resolved: ResolvedQuery, model: SemanticModel | None) -> _Projection:
    """What the outer query can name, read off the resolved query.

    ``model`` is absent only for a caller that built the AST by hand rather
    than compiling one - every pipeline path passes it - and a dimension's
    expression cannot be rebuilt without it, so that caller matches on the
    column form alone.
    """
    by_expr: list[tuple[Expr, str]] = (
        [
            (make_column_expr(model, d.object_name, d.column_name), d.name)
            for d in resolved.dimensions
        ]
        if model is not None
        else []
    )
    by_expr.extend((m.expression, m.name) for m in resolved.measures)
    return _Projection(
        by_column={(d.source_column, d.object_name): d.name for d in resolved.dimensions},
        by_expr=by_expr,
    )


def _outer_expr(expr: Expr, projection: _Projection) -> Expr:
    """One ordering key, as the outer query can read it."""
    if isinstance(expr, Literal):
        # An ordinal position, which resolves against the outer projection
        # unchanged - and is the documented workaround for #358.
        return expr
    for projected, name in projection.by_expr:
        if expr == projected:
            return ColumnRef(name=name)
    if isinstance(expr, ColumnRef):
        # Named by its physical column, so read the alias the projection gave
        # it. An unprojected column keeps its own name and drops the table
        # qualifier, which is what the CTE exposes it under.
        return ColumnRef(name=projection.by_column.get((expr.name, expr.table), expr.name))
    # Nothing the projection names. Left as it stands rather than guessed at:
    # the alternative is inventing an alias for an expression no column holds.
    return expr


__all__ = ["outer_order_by", "remap_order_by"]
