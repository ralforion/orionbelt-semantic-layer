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

Several of these wrappers can run in the same query - totals then window, or
cumulative then window - each nesting the previous one's output. So the
decision cannot be made per wrapper: a wrapper asked only about *its own*
aliases would leave a later wrapper's predicate in its CTE, and re-add an
earlier wrapper's on top. :func:`windowed_aliases` therefore answers the
question once for the whole query, and every wrapper excludes the same set. The
filtering query is applied once, at the end of the pass chain, where every
alias exists (see ``passes.PASS_HAVING_WINDOW``).

``pop_wrap`` materialises every measure in ``pop_compare`` and applies HAVING
as an outer ``WHERE``; it excludes the windowed ones so the final pass owns
them. ``grain_dedup`` hoists its own, into a query where the deduplicated
measures are plain CTE columns and a ``WHERE`` suffices, and never runs
alongside these wrappers.
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
from orionbelt.compiler.resolution import ResolutionError, ResolvedQuery
from orionbelt.models.errors import SemanticError


def _unbindable(unbound: list[str]) -> ResolutionError:
    """A HAVING predicate past a window references something that is not projected.

    The filtering query can only see the columns the windowed query outputs, so
    a predicate naming a dimension or measure that is not selected has nothing
    to bind to. Raised rather than emitted, so the failure is a message about
    the query instead of a database error about an unknown identifier.

    A ``ResolutionError`` rather than a bespoke type, so it surfaces through the
    CLI, REST and pgwire handlers that already translate one.
    """
    listed = ", ".join(repr(name) for name in unbound)
    return ResolutionError(
        [
            SemanticError(
                code="INCOMPATIBLE_COMBINATION",
                message=(
                    f"A HAVING filter on a windowed measure also references {listed}, "
                    f"which is not available where the filter has to be evaluated. The "
                    f"predicate runs after the window function, over the query's own "
                    f"output, so it can only use selected dimensions and measures."
                ),
                path="having",
                hint=(
                    "Add the referenced dimension to the query's select, or move that "
                    "part of the filter to 'where'."
                ),
                context={"unavailable": unbound},
            )
        ]
    )


def windowed_aliases(resolved: ResolvedQuery) -> set[str]:
    """Every alias whose value a window wrapper produces in this query.

    One answer for the whole query, covering all three wrappers, because they
    nest: a query with both a ``total: true`` measure and a rank metric runs
    totals and then window, and neither may leave the other's predicate behind
    in its CTE.

    Derived from *resolved* alone, so it does not depend on which passes the
    compatibility rules end up suppressing. A suppressed wrapper leaves the
    measure un-windowed, and filtering that plain aggregate in the outer query
    is equivalent to filtering it in HAVING, so over-inclusion here is safe.
    """
    # Local imports: both modules import this one for the split.
    from orionbelt.compiler.total_wrap import (
        _metrics_with_total_components,
        _needs_window_wrap,
    )
    from orionbelt.compiler.window_wrap import _ddm_window_components

    names = set(_metrics_with_total_components(resolved))
    for measure in resolved.measures:
        is_windowed = (
            # window_wrap: a rank / lag / lead metric, or a derived metric
            # whose expression contains one.
            measure.is_window
            or bool(_ddm_window_components(measure, resolved.metric_components))
            # cumulative_wrap: a running total.
            or measure.is_cumulative
            # total_wrap: a direct measure with total: true or a grain override.
            or (
                not measure.component_measures
                and _needs_window_wrap(measure, resolved.dedup_measures)
            )
        )
        if is_windowed:
            names.add(measure.name)
    return names


def _combine(predicates: list[Expr]) -> Expr | None:
    combined: Expr | None = None
    for predicate in predicates:
        combined = predicate if combined is None else BinaryOp(combined, "AND", predicate)
    return combined


def _alias_of(column: Expr) -> str | None:
    return column.alias if isinstance(column, AliasedExpr) else None


def inner_having(ast: Select, resolved: ResolvedQuery) -> Expr | None:
    """The HAVING a window wrapper may keep in its CTE.

    Everything referencing a windowed alias is dropped, whichever wrapper
    produces it, so no wrapper leaves a later one's predicate behind or re-adds
    an earlier one's. The dropped predicates are applied once at the end of the
    pass chain by :func:`apply_having_hoist`.

    ``resolved.having_filters`` carry the measure as a bare ``ColumnRef``;
    ``star.py`` expands that into the aggregate when it emits HAVING, so a
    predicate staying inside the CTE needs the same expansion, taken from the
    planner's own column expressions on *ast*.

    Returns ``ast.having`` untouched when nothing is windowed, so a query with
    no such predicate compiles byte-for-byte as before.
    """
    windowed = windowed_aliases(resolved)
    if not any(hf.referenced_fields & windowed for hf in resolved.having_filters):
        return ast.having

    planner_exprs = {
        alias: column.expr
        for column in ast.columns
        if (alias := _alias_of(column)) is not None and isinstance(column, AliasedExpr)
    }
    return _combine(
        [
            _expand(hf.expression, planner_exprs)
            for hf in resolved.having_filters
            if not hf.referenced_fields & windowed
        ]
    )


def hoisted_predicates(resolved: ResolvedQuery) -> list[Expr]:
    """The HAVING predicates that must be evaluated after the window functions.

    A predicate may also constrain a dimension, which the planner resolved to a
    *physical* column (``"Sales"."cls"``). The base tables are long out of scope
    by the time this runs, so those refs are rewritten to the dimension's output
    alias, which every wrapper projects. Anything left unresolvable is caught by
    :func:`apply_having_hoist`.
    """
    windowed = windowed_aliases(resolved)
    by_physical = {
        (dim.source_column, dim.object_name): dim.name
        for dim in resolved.dimensions
        if dim.source_column
    }

    def to_alias(ref: ColumnRef) -> Expr:
        if ref.table is None:
            return ref
        alias = by_physical.get((ref.name, ref.table))
        return ColumnRef(name=alias) if alias is not None else ref

    return [
        map_column_refs(hf.expression, to_alias)
        for hf in resolved.having_filters
        if hf.referenced_fields & windowed
    ]


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
    unbound = sorted(_unbound_refs(_combine(hoisted), projected))
    if unbound:
        raise _unbindable(unbound)

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


def _unbound_refs(expr: Expr | None, projected: set[str]) -> set[str]:
    """References in *expr* that the wrapping query cannot resolve.

    A bare ref binds when the alias is projected. A *qualified* one never
    binds: it names a base table that is several CTEs out of scope by now, so
    it counts as unbound whatever it is called. Reporting it as
    ``"Sales"."cls"`` is deliberate - that is the form the modeller sees in the
    generated SQL.
    """
    found: set[str] = set()

    def note(ref: ColumnRef) -> ColumnRef:
        if ref.table is not None:
            found.add(f'"{ref.table}"."{ref.name}"')
        elif ref.name not in projected:
            found.add(ref.name)
        return ref

    if expr is not None:
        map_column_refs(expr, note)
    return found
