"""Move HAVING predicates past a window wrapper.

Several wrappers rewrite the planner's query as ``WITH base AS (<grouped
query>) SELECT ..., <window fn> OVER (...) FROM base``: ``total_wrap`` for
``total: true`` and grain overrides, ``window_wrap`` for rank/lag/lead metrics,
``cumulative_wrap`` for running totals. In every one, the measure's *final*
value is produced by the window in the outer query, not by the aggregate in the
CTE.

A HAVING predicate on such a measure therefore cannot stay where the planner
put it. Left in the CTE it is evaluated against the pre-window aggregate, so
the same alias means two different things in one statement and the wrong rows
come back with no error raised. For a rank the predicate is worse than wrong:
the window function is not in the CTE at all, so the comparison silently binds
to the underlying measure instead.

The predicate has to run after the window, which SQL allows in exactly two
places: ``QUALIFY``, which only three of the eight dialects here declare, or a
wrapping ``SELECT`` over the windowed rows, which all eight support. This
module does the latter, so one code path serves every dialect.

``pop_wrap`` needs none of this - it materialises every measure in
``pop_compare`` and already applies all HAVING filters as an outer ``WHERE``.
``grain_dedup`` hoists its own, into a query where the deduplicated measures
are plain CTE columns and a ``WHERE`` suffices.
"""

from __future__ import annotations

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    Select,
)
from orionbelt.compiler.expr_rewrite import map_column_refs
from orionbelt.compiler.resolution import ResolvedQuery


class HavingHoistError(Exception):
    """A HAVING predicate past a window references something that is not projected.

    The wrapping query can only see the columns the windowed query outputs, so
    a predicate naming a measure or dimension that is not selected has nothing
    to bind to. Raised rather than emitted, so the failure is a message about
    the query instead of a database error about an unknown identifier.
    """


def _combine(predicates: list[Expr]) -> Expr | None:
    combined: Expr | None = None
    for predicate in predicates:
        combined = predicate if combined is None else BinaryOp(combined, "AND", predicate)
    return combined


def _alias_of(column: Expr) -> str | None:
    return column.alias if isinstance(column, AliasedExpr) else None


def split_having(
    ast: Select,
    resolved: ResolvedQuery,
    windowed: set[str],
    *,
    expand: bool = True,
) -> tuple[Expr | None, list[Expr]]:
    """Split HAVING into what stays in the CTE and what must run after the window.

    Returns ``(inner_having, hoisted)``. *windowed* names the aliases this
    wrapper computes with a window function.

    ``resolved.having_filters`` carry the measure as a bare ``ColumnRef``;
    ``star.py`` expands that into the aggregate when it emits HAVING. A
    predicate that stays inside the CTE needs the same expansion, which is why
    the planner's own column expressions are read back off *ast*. A hoisted one
    needs no expansion at all: the wrapping query reads the measure as a
    materialised column under exactly that alias.

    When nothing is hoisted, ``ast.having`` is returned untouched, so a query
    without a windowed predicate compiles byte-for-byte as before.
    """
    hoisted_filters = [hf for hf in resolved.having_filters if hf.referenced_fields & windowed]
    if not hoisted_filters:
        return ast.having, []

    planner_exprs = {
        alias: column.expr
        for column in ast.columns
        if (alias := _alias_of(column)) is not None and isinstance(column, AliasedExpr)
    }

    inner = [
        _expand(hf.expression, planner_exprs) if expand else hf.expression
        for hf in resolved.having_filters
        if not hf.referenced_fields & windowed
    ]
    return _combine(inner), [hf.expression for hf in hoisted_filters]


def _expand(expr: Expr, by_alias: dict[str, Expr]) -> Expr:
    """Replace bare alias references with the planner's expression for them."""
    return map_column_refs(
        expr,
        lambda ref: by_alias.get(ref.name, ref) if ref.table is None else ref,
    )


def apply_having_hoist(
    outer: Select,
    hoisted: list[Expr],
    *,
    cte_name: str,
) -> Select:
    """Nest *outer* in a CTE and filter its rows, so *hoisted* sees window values.

    ``ORDER BY`` / ``LIMIT`` / ``OFFSET`` move out to the filtering query: a
    limit applied before the predicate would count rows the caller asked to
    exclude.
    """
    if not hoisted:
        return outer

    projected = {alias for column in outer.columns if (alias := _alias_of(column)) is not None}
    referenced = sorted(_bare_refs(_combine(hoisted)) - projected)
    if referenced:
        listed = ", ".join(repr(name) for name in referenced)
        raise HavingHoistError(
            f"A HAVING filter on a windowed measure also references {listed}, which "
            f"the query does not select. The predicate has to be evaluated after the "
            f"window function, where only selected columns exist. Add {listed} to the "
            f"query's select, or move that part of the filter to 'where'."
        )

    inner = Select(
        columns=outer.columns,
        from_=outer.from_,
        joins=outer.joins,
        where=outer.where,
        group_by=outer.group_by,
        having=outer.having,
        ctes=[],
        distinct=outer.distinct,
        grouping=outer.grouping,
    )
    return Select(
        columns=[
            AliasedExpr(expr=ColumnRef(name=alias, table=cte_name), alias=alias)
            for column in outer.columns
            if (alias := _alias_of(column)) is not None
        ],
        from_=From(source=cte_name, alias=cte_name),
        where=_combine(hoisted),
        order_by=outer.order_by,
        limit=outer.limit,
        offset=outer.offset,
        ctes=[*outer.ctes, CTE(name=cte_name, query=inner)],
    )


def _bare_refs(expr: Expr | None) -> set[str]:
    """Names of every table-less ``ColumnRef`` in *expr*."""
    found: set[str] = set()

    def note(ref: ColumnRef) -> ColumnRef:
        if ref.table is None:
            found.add(ref.name)
        return ref

    if expr is not None:
        map_column_refs(expr, note)
    return found
