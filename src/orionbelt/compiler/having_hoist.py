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

Several wrappers can run in one query - dedup then totals then window, filter
context then totals - each nesting the previous one's output, and each copying
or rebuilding ``ast.having`` differently. Stripping the predicate per wrapper
therefore does not hold: any wrapper that kept a stale copy would filter
pre-window behind this module's back, which is how ``filter_wrap`` and
``grain_dedup`` both ended up double-filtering.

So the predicate is withheld at the source instead. ``star.py`` never emits a
HAVING on a windowed alias, so no wrapper can carry one, and
``passes.PASS_HAVING_WINDOW`` applies them once at the end of the chain where
every alias exists. :func:`windowed_aliases` is the single answer to "which
aliases does a wrapper finish with a window function", shared by the planner,
``grain_dedup``, ``pop_wrap`` and the pass itself.

``grain_dedup`` hoists its *own* predicates separately, into a query where the
deduplicated measures are plain CTE columns and a ``WHERE`` suffices. The two
sets are disjoint: :func:`windowed_aliases` excludes deduplicated measures via
``total_wrap._needs_window_wrap``.
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

    Over-inclusion is *not* safe, so this tracks pass suppression. The totals
    pass is skipped when combined with PoP or cumulative (``passes`` rule 2),
    which leaves ``total: true`` as an ordinary aggregate. Treating it as
    windowed anyway would move its predicate past the cumulative window, and a
    running total accumulates over whichever rows survive HAVING - so the
    filter has to stay in the grouped query or the totals themselves come out
    wrong.
    """
    # Local imports: both modules import this one for the split.
    from orionbelt.compiler.total_wrap import (
        _metrics_with_total_components,
        _needs_window_wrap,
    )
    from orionbelt.compiler.window_wrap import _ddm_window_components

    # Mirrors ``evaluate_compatibility`` rule 2: PASS_TOTALS is suppressed when
    # PASS_PERIOD_OVER_PERIOD or PASS_CUMULATIVE also applies.
    totals_run = resolved.has_totals and not (resolved.has_pop or resolved.has_cumulative)

    names = set(_metrics_with_total_components(resolved)) if totals_run else set()
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
                totals_run
                and not measure.component_measures
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
