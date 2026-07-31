"""Expanding a metric's ``{[Name]}`` placeholders into the values they name.

A metric formula parses to an AST whose leaves are table-less ``ColumnRef``
nodes carrying component names. Turning that into SQL means replacing each leaf
with the component's value — its aggregate for the star planner, a CTE column
for the wrappers that computed it earlier.

Two rules make that replacement correct, and both are the reason this lives in
one place rather than being re-derived per pass:

* A component that is itself a **derived** metric is expanded *recursively*.
  Substituting one level only left the inner metric's own placeholders in the
  output as bare column names, which no engine can bind.
* A component that is a **cumulative**, **window**, or **period-over-period**
  metric is not. Those are computed by their own wrapper, which projects the
  value under the component's name in a CTE beneath the current query, so the
  reference has to survive as a name for that wrapper to resolve.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import ColumnRef, Expr
from orionbelt.compiler.expr_rewrite import map_column_refs

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import ResolvedMeasure


def expand_metric_expression(
    expr: Expr,
    components: Mapping[str, ResolvedMeasure],
    value_of: Callable[[ResolvedMeasure], Expr],
    _seen: frozenset[str] = frozenset(),
) -> Expr:
    """Replace every component placeholder in *expr* with its value.

    *value_of* supplies the value for a component the expansion stops at: the
    star planner passes the component's aggregate expression, a wrapper passes
    the CTE column it projected. Nested derived metrics are expanded first, so
    *value_of* only ever sees a measure or a wrapper-computed metric.

    *_seen* guards a metric that reaches itself. The parser cannot express one
    (a reference must name a metric already defined above it), so this only
    keeps a hand-built model from hanging the compiler.
    """

    def visit(ref: ColumnRef) -> Expr:
        if ref.table is not None:
            return ref
        component = components.get(ref.name)
        if component is None:
            return ref
        if component.is_derived_metric:
            if ref.name in _seen:
                return ref
            return expand_metric_expression(
                component.expression, components, value_of, _seen | {ref.name}
            )
        return value_of(component)

    return map_column_refs(expr, visit)


def metric_leaf_measure_names(
    measure: ResolvedMeasure,
    components: Mapping[str, ResolvedMeasure],
) -> list[str]:
    """Names of the *measures* a metric ultimately reads.

    Goes one step further than :func:`metric_leaf_components`: a cumulative,
    window, or period-over-period component resolves to the base measure it is
    computed over, since that is the measure a caller checking against the model
    cares about. ``fanout`` is that caller — a metric's window component is not
    in ``effective_measures``, so stopping at its name skipped the base measure
    entirely and the query's row multiplication went unreported.

    A name the resolver never put in *components* is kept as-is rather than
    dropped, so an incomplete map cannot silently shrink the checked set.
    """
    out: list[str] = []
    seen: set[str] = set()

    def walk(current: ResolvedMeasure) -> None:
        for name in current.component_measures:
            if name in seen:
                continue
            seen.add(name)
            component = components.get(name)
            if component is None:
                out.append(name)
            elif component.is_derived_metric:
                walk(component)
            elif component.component_measures:
                # Wrapper-computed: recurse into whichever measure it wraps.
                walk(component)
            else:
                out.append(name)

    walk(measure)
    return out


def metric_leaf_components(
    measure: ResolvedMeasure,
    components: Mapping[str, ResolvedMeasure],
) -> list[ResolvedMeasure]:
    """Components *measure* ultimately reads, following nested derived metrics.

    Stops at the same boundary :func:`expand_metric_expression` does: a measure,
    or a metric a wrapper computes. Order is first-seen, so callers that project
    these get a stable column order.
    """
    out: list[ResolvedMeasure] = []
    seen: set[str] = set()

    def walk(current: ResolvedMeasure) -> None:
        for name in current.component_measures:
            component = components.get(name)
            if component is None or name in seen:
                continue
            seen.add(name)
            if component.is_derived_metric:
                walk(component)
            else:
                out.append(component)

    walk(measure)
    return out
