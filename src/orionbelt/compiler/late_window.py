"""Window metrics over a reaggregate metric, computed after the reaggregate pass.

A window metric (rank / lag / lead / ntile / first / last value) applies its
window function over the query's result rows, and the window pass does so over
each measure's column at the query grain. A reaggregate metric has no such
column until the reaggregate pass, which runs last, has joined its two stages
back: before it, every wrapper carries a placeholder. So a window over a
reaggregate metric is left alone by the window pass and computed here, over
the finished rows, and so is a derived metric whose formula reads one (``Avg
per Customer - Avg per Customer Prior Month``).

The values these read are the finished columns of the reaggregate pass. Those
that are not selected - a window's base reaggregate metric, the other measures
of a formula - are included by resolution as hidden measures
(``ResolvedQuery.late_window_inputs``) and dropped from the projection here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import CTE, AliasedExpr, ColumnRef, Expr, From, Select
from orionbelt.compiler.metric_expansion import expand_metric_expression, metric_leaf_components
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.type_resolver import resolve_metric_data_type
from orionbelt.compiler.window_wrap import _apply_metric_cast, _build_window_call

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import SemanticModel

_BASE = "late_window_base"


def is_late(measure: ResolvedMeasure, components: dict[str, ResolvedMeasure]) -> bool:
    """A window over a reaggregate metric, or a derived metric reading one."""
    if measure.window_over_reaggregate:
        return True
    if not measure.is_derived_metric:
        return False
    return any(c.window_over_reaggregate for c in metric_leaf_components(measure, components))


def late_inputs(measure: ResolvedMeasure, components: dict[str, ResolvedMeasure]) -> list[str]:
    """The finished columns *measure* is computed from here: each window's base
    reaggregate metric, and a formula's other leaf measures."""
    leaves = [measure] if measure.is_window else metric_leaf_components(measure, components)
    names: list[str] = []
    for leaf in leaves:
        name = leaf.window_base_measure if leaf.window_over_reaggregate else leaf.name
        if name and name not in names:
            names.append(name)
    return names


def late_window_applies(resolved: ResolvedQuery) -> bool:
    return any(is_late(m, resolved.metric_components) for m in resolved.measures)


def wrap_with_late_window(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Select:
    """The finished result as a CTE, and over it each late metric's window.

    The windows run over every row of the result, so the CTE drops the query's
    ORDER BY, LIMIT and OFFSET and the outer query applies them, as the window
    pass does.
    """
    components = resolved.metric_components
    late = [m for m in resolved.measures if is_late(m, components)]
    if not late:
        return ast

    def window_over(window: ResolvedMeasure) -> Expr:
        call = _build_window_call(window, model, dialect)
        return _apply_metric_cast(call, window.name, model, dialect)

    def value_of(comp: ResolvedMeasure) -> Expr:
        if comp.window_over_reaggregate:
            return window_over(comp)
        return ColumnRef(name=comp.name)

    hidden = resolved.late_window_inputs
    columns: list[Expr] = [
        AliasedExpr(expr=ColumnRef(name=d.name), alias=d.name) for d in resolved.dimensions
    ]
    for m in resolved.measures:
        if m.name in hidden:
            continue
        value: Expr
        if m.window_over_reaggregate:
            value = window_over(m)
        elif m in late:
            value = expand_metric_expression(m.expression, components, value_of)
            metric = model.metrics.get(m.name) if model is not None else None
            if metric is not None and dialect is not None:
                target = resolve_metric_data_type(metric, model.settings if model else None)
                if target is not None:
                    value = dialect.cast_to_obml_type(value, target)
        else:
            value = ColumnRef(name=m.name)
        columns.append(AliasedExpr(expr=value, alias=m.name))

    base = CTE(
        name=_BASE,
        query=replace(ast, order_by=[], limit=None, offset=None, ctes=[]),
    )
    return Select(
        columns=columns,
        from_=From(source=_BASE, alias=_BASE),
        order_by=outer_order_by(resolved, model),
        limit=ast.limit,
        offset=ast.offset,
        ctes=[*ast.ctes, base],
    )
