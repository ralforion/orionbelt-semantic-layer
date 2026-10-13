"""Window and cumulative metrics over a reaggregate metric, after the reaggregate pass.

A window metric (rank / lag / ...) applies its window over the query's result
rows, a cumulative metric (running, grain-to-date, rolling) over the periods of
its time dimension, and the window and cumulative passes do so over each
measure's column at the query grain. A reaggregate metric has no such column
until the reaggregate pass, which runs last, has joined its two stages back:
before it, every wrapper carries a placeholder. So these metrics are left alone
by those passes and computed here, over the finished rows, and so is a derived
metric whose formula reads one (``Avg per Customer - Avg per Customer Prior
Month``).

The values they read are the finished columns of the reaggregate pass. Those
that are not selected - a metric's base reaggregate metric, the other measures
of a formula - are included by resolution as hidden measures
(``ResolvedQuery.late_window_inputs``) and dropped from the projection here.

A cumulative metric reads past a filter on its time dimension, as every
cumulative metric does: a running total shown from March still adds January.
The finished rows stop at the filter, so then the reaggregate metric is
planned again, as a query of its own at the query's dimensions without the
filter, and the window runs over that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    Join,
    JoinType,
    Select,
    Unnest,
)
from orionbelt.compiler.having_hoist import windowed_aliases
from orionbelt.compiler.metric_expansion import expand_metric_expression, metric_leaf_components
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.time_lookback import null_safe_eq
from orionbelt.compiler.type_resolver import resolve_metric_data_type
from orionbelt.compiler.window_wrap import _apply_metric_cast, _build_window_call
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
    QuerySelect,
)

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import DataObject, SemanticModel

_BASE = "late_window_base"
_JOINED = "late_window_joined"


def _over_reaggregate(measure: ResolvedMeasure) -> bool:
    return measure.window_over_reaggregate or measure.cumulative_over_reaggregate


def is_late(measure: ResolvedMeasure, components: dict[str, ResolvedMeasure]) -> bool:
    """A window or cumulative metric over a reaggregate metric, or a derived
    metric reading one."""
    if _over_reaggregate(measure):
        return True
    if not measure.is_derived_metric:
        return False
    return any(_over_reaggregate(c) for c in metric_leaf_components(measure, components))


def late_inputs(measure: ResolvedMeasure, components: dict[str, ResolvedMeasure]) -> list[str]:
    """The finished columns *measure* is computed from here: each window's or
    cumulative's base reaggregate metric, and a formula's other leaf measures."""
    leaves = (
        [measure] if _over_reaggregate(measure) else metric_leaf_components(measure, components)
    )
    names: list[str] = []
    for leaf in leaves:
        if leaf.window_over_reaggregate:
            name = leaf.window_base_measure
        elif leaf.cumulative_over_reaggregate:
            name = leaf.cumulative_measure
        else:
            name = leaf.name
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
    query: QueryObject | None = None,
    qualify_table: Callable[[DataObject], str] | None = None,
) -> Select:
    """The finished result as a CTE, and over it each late metric.

    The windows run over every row of the result, so the CTE drops the query's
    ORDER BY, LIMIT and OFFSET and the outer query applies them, as the window
    pass does. Cumulative values are computed apart and joined back on the
    dimensions.
    """
    # The placeholders every pass before this one read give way to the real
    # formulas, for the formulas selected and those nested in them.
    components = {
        name: replace(c, expression=c.deferred_expression)
        if c.deferred_expression is not None
        else c
        for name, c in resolved.metric_components.items()
    }
    late = [m for m in resolved.measures if is_late(m, components)]
    if not late:
        return ast

    cumulatives: dict[str, ResolvedMeasure] = {}
    for m in late:
        leaves = [m] if _over_reaggregate(m) else metric_leaf_components(m, components)
        for leaf in leaves:
            if leaf.cumulative_over_reaggregate:
                cumulatives.setdefault(leaf.name, leaf)

    ctes: list[CTE] = [
        CTE(name=_BASE, query=replace(ast, order_by=[], limit=None, offset=None, ctes=[]))
    ]
    source = _BASE
    if cumulatives:
        if model is None or dialect is None or query is None or qualify_table is None:
            raise ValueError("A cumulative metric over a reaggregate metric needs the query")
        ctes += _cumulative_ctes(
            ast, resolved, list(cumulatives.values()), model, dialect, query, qualify_table
        )
        source = _JOINED

    def window_over(window: ResolvedMeasure) -> Expr:
        call = _build_window_call(window, resolved, model, dialect)
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
        elif m.cumulative_over_reaggregate:
            value = _apply_metric_cast(ColumnRef(name=m.name), m.name, model, dialect)
        elif m in late:
            formula = m.deferred_expression if m.deferred_expression is not None else m.expression
            value = expand_metric_expression(formula, components, value_of)
            metric = model.metrics.get(m.name) if model is not None else None
            if metric is not None and dialect is not None:
                target = resolve_metric_data_type(metric, model.settings if model else None)
                if target is not None:
                    value = dialect.cast_to_obml_type(value, target)
        else:
            value = ColumnRef(name=m.name)
        columns.append(AliasedExpr(expr=value, alias=m.name))

    return Select(
        columns=columns,
        from_=From(source=source, alias=source),
        order_by=outer_order_by(resolved, model),
        limit=ast.limit,
        offset=ast.offset,
        ctes=[*ast.ctes, *ctes],
    )


def _cumulative_ctes(
    ast: Select,
    resolved: ResolvedQuery,
    cumulatives: list[ResolvedMeasure],
    model: SemanticModel,
    dialect: Dialect,
    query: QueryObject,
    qualify_table: Callable[[DataObject], str],
) -> list[CTE]:
    """Each cumulative's values, and the finished rows with them joined in.

    A cumulative reads the finished rows, unless the query filters its time
    dimension's column: then the base reaggregate metric is planned again at
    the query's dimensions without those filters, as the cumulative pass's
    look-back reads the query without them. A rolling window counts calendar
    periods with the cumulative pass's own self-join.
    """
    # Imported here: cumulative_wrap imports window_wrap, and the pipeline owns
    # the phase order (both import this module through ``passes``).
    from orionbelt.compiler.cumulative_wrap import (
        _is_rolling,
        _partition_names,
        _rolling_query,
        _window_for,
    )
    from orionbelt.compiler.pipeline import plan_in_own_right
    from orionbelt.compiler.reaggregate_wrap import _own_cte_names

    dim_names = [d.name for d in resolved.dimensions]
    # A HAVING the main query evaluates after a wrapper finished its value - a
    # reaggregate, cumulative, window or total - filters the shown rows, not
    # the periods read: the having hoist's own classification says which.
    deferred = windowed_aliases(resolved)
    ctes: list[CTE] = []
    value_of: dict[str, str] = {}
    for index, m in enumerate(cumulatives, 1):
        assert m.cumulative_time_dimension is not None and m.cumulative_measure is not None
        source = _BASE
        where = _without_axis(query.where, m.cumulative_time_dimension, resolved, model)
        if where != list(query.where):
            source = f"late_lookback_{index}"
            lookback = QueryObject(
                select=QuerySelect(
                    dimensions=list(query.select.dimensions), measures=[m.cumulative_measure]
                ),
                where=where,
                having=_periods_having(list(query.having), deferred),
                use_path_names=list(query.use_path_names),
                allow_fan_out=query.allow_fan_out,
            )
            scan, _ = plan_in_own_right(lookback, model, dialect, qualify_table)
            ctes.append(CTE(name=source, query=_own_cte_names(scan, f"{source}_")))
        name = f"late_cumulative_{index}"
        if _is_rolling(m):
            partitions = _partition_names(m, resolved)
            value = _rolling_query(source, [m], m.cumulative_time_dimension, partitions, resolved)
        else:
            value = Select(
                columns=[
                    *(AliasedExpr(expr=ColumnRef(name=d), alias=d) for d in dim_names),
                    AliasedExpr(expr=_window_for(m, resolved, model, dialect), alias=m.name),
                ],
                from_=From(source=source, alias=source),
            )
        ctes.append(CTE(name=name, query=value))
        value_of[m.name] = name

    columns: list[Expr] = []
    for col in ast.columns:
        alias = col.alias if isinstance(col, AliasedExpr) else None
        if alias is None or alias in value_of:
            continue
        columns.append(AliasedExpr(expr=ColumnRef(table=_BASE, name=alias), alias=alias))
    columns += [
        AliasedExpr(expr=ColumnRef(table=table, name=name), alias=name)
        for name, table in value_of.items()
    ]
    joins: list[Join | Unnest] = []
    for table in value_of.values():
        on: Expr | None = None
        for d in dim_names:
            eq = null_safe_eq(ColumnRef(table=_BASE, name=d), ColumnRef(table=table, name=d))
            on = eq if on is None else _and(on, eq)
        joins.append(Join(join_type=JoinType.LEFT, source=table, alias=table, on=on))
    ctes.append(
        CTE(
            name=_JOINED,
            query=Select(columns=columns, from_=From(source=_BASE, alias=_BASE), joins=joins),
        )
    )
    return ctes


def _and(left: Expr, right: Expr) -> Expr:
    return BinaryOp(left=left, op="AND", right=right)


def _without_axis(
    filters: list[QueryFilter | QueryFilterGroup],
    time_dim_name: str,
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> list[QueryFilter | QueryFilterGroup]:
    """*filters* without those that only pick periods of the time dimension.

    The rule the cumulative pass's ``time_filters`` applies to the resolved
    filters, at the query's: a top-level filter is a time filter when every
    leaf in it tests the time dimension's own column (the dimension, or any
    dimension or qualified column over the same date column), whatever its
    logic or negation; a group mixing columns is not one, and is never split,
    since removing part of a negated group changes what the rest means. An
    ``exists`` / ``nonexists`` filter tests a relationship, not a period, and
    always stays.
    """
    time_dim = next((d for d in resolved.dimensions if d.name == time_dim_name), None)
    if time_dim is None or time_dim.via is not None:
        return list(filters)
    axis = (time_dim.object_name, time_dim.column_name)

    def subject(item: QueryFilter | QueryFilterGroup) -> tuple[str, str] | None:
        if isinstance(item, QueryFilterGroup):
            subjects = {subject(child) for child in item.filters}
            return next(iter(subjects)) if len(subjects) == 1 else None
        if item.op in (FilterOperator.EXISTS, FilterOperator.NONEXISTS):
            return None
        name = item.field.split(":", 1)[0]
        dimension = model.dimensions.get(name)
        if dimension is not None:
            if dimension.via is not None or dimension.view is None or dimension.column is None:
                return None
            return (dimension.view, dimension.column)
        if "." in item.field:
            obj, col = (part.strip() for part in item.field.split(".", 1))
            return (obj, col)
        return None

    return [f for f in filters if subject(f) != axis]


def _periods_having(
    having: list[QueryFilter | QueryFilterGroup], deferred: set[str]
) -> list[QueryFilter | QueryFilterGroup]:
    """The query's HAVING for the look-back: the conditions evaluated in the
    grouped query pick which periods there are, so the look-back keeps them;
    one on a value a wrapper finishes (*deferred*, the having hoist's
    ``windowed_aliases``) filters the shown rows afterwards, and is left out.
    A group mentioning one is hoisted whole, so it is left out whole."""

    def mentions(item: QueryFilter | QueryFilterGroup) -> bool:
        if isinstance(item, QueryFilterGroup):
            return any(mentions(child) for child in item.filters)
        return item.field in deferred

    return [h for h in having if not mentions(h)]
