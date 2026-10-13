"""Two-stage aggregation for ``MetricType.REAGGREGATE``.

A reaggregate metric is its measure computed at the query grain plus the
``per`` dimensions (stage 1), then aggregated again to the query grain
(stage 2) - "average revenue per customer, by country" is ``SUM(revenue)`` per
country and customer, then ``AVG`` of those per country.

The plan carries the measure's aggregate under the metric's name at the query
grain, which is a placeholder and not the metric; every other wrapper carries
it like any measure. This wrapper runs after all of them, moves the finished
query into a ``reagg_base`` CTE without that column, and per distinct
(measure, ``per``, ``having``) adds:

- ``reagg_<n>_inner``: the measure planned *as a query in its own right* at the
  finer grain, so its base object, join path and fanout check are derived for
  that grain (the same route ``filter_wrap`` takes for a filterContext scan),
  with the metric's ``having`` as that query's HAVING;
- ``reagg_<n>``: the second-stage aggregate of that scan, grouped by the query
  dimensions.

The outer query reads ``reagg_base`` and LEFT JOINs each ``reagg_<n>`` on the
query dimensions, NULL-safely, since a NULL dimension value is a group of its
own. Both stages see the query's WHERE, so every group of ``reagg_base`` has
rows in stage 1, unless ``having`` removed them all: the join then finds no
row and the metric is NULL, or 0 for ``count``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import count
from typing import NoReturn

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    Select,
    UnionAll,
    Unnest,
    WindowFrame,
    WindowFunction,
)
from orionbelt.compiler.filters import _resolve_subquery_filter_field
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.late_window import is_late
from orionbelt.compiler.metric_expansion import metric_leaf_components, metric_over_components
from orionbelt.compiler.metric_resolution import (
    _GRAIN_RANK,
    reaggregate_per_covered,
    reaggregate_per_ref,
)
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import (
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    column_name,
    grain_column,
)
from orionbelt.compiler.time_lookback import (
    lookback_query,
    null_safe_eq,
    rename_cte_sources,
    time_filters,
)
from orionbelt.compiler.type_resolver import (
    exact_reaggregate_avg,
    reaggregated_data_type,
    reaggregated_values_are_integers,
    resolve_metric_data_type,
)
from orionbelt.compiler.window_wrap import partition_keys
from orionbelt.dialect.base import Dialect
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    CoalesceDimension,
    DimensionRef,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
    QuerySelect,
    time_per_entries,
)
from orionbelt.models.semantic import (
    DATE_BEARING_TYPES,
    AggregationType,
    DataObject,
    FilterLogic,
    Measure,
    Metric,
    MetricType,
    ReaggregateAggType,
    ReaggregateHaving,
    ReaggregatePopulation,
    SemanticModel,
    TimeGrain,
)
from orionbelt.models.types import OBMLType

_BASE = "reagg_base"
_COMPONENTS = "reagg_components"

_FUNCTIONS: dict[ReaggregateAggType, str] = {
    ReaggregateAggType.SUM: "SUM",
    ReaggregateAggType.AVG: "AVG",
    ReaggregateAggType.MIN: "MIN",
    ReaggregateAggType.MAX: "MAX",
    ReaggregateAggType.COUNT: "COUNT",
    ReaggregateAggType.MEDIAN: "MEDIAN",
    ReaggregateAggType.PERCENTILE_CONT: "PERCENTILE_CONT",
    ReaggregateAggType.PERCENTILE_DISC: "PERCENTILE_DISC",
    ReaggregateAggType.FIRST: "FIRST",
    ReaggregateAggType.LAST: "LAST",
}


#: The prefix ``_own_cte_names`` gives a stage-1 CTE, which a further stage
#: replaces rather than adds to.
_STAGE_CTE_PREFIX = re.compile(r"^reagg_\d+_inner_\d+_")


def _having(model: SemanticModel, name: str) -> list[ReaggregateHaving]:
    """The reaggregate metric *name*'s conditions on its first stage."""
    metric = model.metrics.get(name)
    return metric.having if metric is not None else []


def _population(model: SemanticModel, name: str) -> ReaggregatePopulation:
    """Where the reaggregate metric *name*'s first-stage groups come from."""
    metric = model.metrics.get(name)
    return (metric.population if metric is not None else None) or ReaggregatePopulation.FACTS


def _carry_forward(model: SemanticModel, name: str) -> bool:
    """Whether the reaggregate metric *name* carries a member's value forward."""
    metric = model.metrics.get(name)
    return metric is not None and metric.carry_forward


def _having_key(having: list[ReaggregateHaving]) -> str:
    return "\n".join(h.model_dump_json() for h in having)


def _alias(expr: Expr) -> str | None:
    return expr.alias if isinstance(expr, AliasedExpr) else None


def _stage_one(
    measure: str,
    per: list[str],
    having: list[ReaggregateHaving],
    population: ReaggregatePopulation,
    carry: bool,
    resolved: ResolvedQuery,
    query: QueryObject,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
) -> tuple[Select, dict[str, str]]:
    """The measure at the query grain plus *per*, planned as a query of its own,
    and the column each ``per`` entry it added groups by.

    Only the query's row filters carry over: its HAVING, ORDER BY and LIMIT
    describe the final result, not this scan. The scan's HAVING is the
    metric's own *having*.

    A ``per`` entry the query's grouping already covers is left out, and so is
    one naming the same bucket as an earlier entry.

    With *population* ``dimension`` the groups are every member of the ``per``
    dimensions instead of the ones the measure has rows for (see
    :func:`_with_population`), and with *carry* a member with no row in a
    period takes its latest earlier value, read past the query's time filter
    (see :func:`_history`).
    """
    added: list[tuple[str, DimensionRef]] = []
    for entry in per:
        ref = reaggregate_per_ref(model, entry)
        if reaggregate_per_covered(model, ref, resolved.dimensions) or any(
            ref == other for _, other in added
        ):
            continue
        added.append((entry, ref))
    plan_model, planned_dims, synthetic = _distinct_axes(
        measure, [*query.select.dimensions, *(entry for entry, _ in added)], model
    )
    sub_query = QueryObject(
        select=QuerySelect(dimensions=planned_dims, measures=[measure]),
        where=list(query.where),
        having=[QueryFilter(field=h.field, op=h.op, value=h.value) for h in having],
        use_path_names=list(query.use_path_names),
        allow_fan_out=query.allow_fan_out,
    )
    # Imported here: the pipeline owns the phase order, and importing it at
    # module scope would be a cycle back through ``compiler.passes``.
    from orionbelt.compiler.pipeline import plan_in_own_right

    try:
        scan, sub_resolved = plan_in_own_right(sub_query, plan_model, dialect, qualify_table)
    except ResolutionError as exc:
        # The dimensions such an error names are the scan's, not the query's.
        raise ResolutionError(
            [
                e.model_copy(
                    update={
                        "message": f"In the first stage of reaggregating '{measure}' "
                        f"per {per}: {e.message}"
                    }
                )
                for e in exc.errors
            ]
        ) from exc
    # Its warnings are about this query - a fan trap in the scan is one here.
    resolved.warnings.extend(sub_resolved.warnings)
    # So are its tables: a ``per`` dimension can join one the query does not,
    # and the result cache has to see it change.
    resolved.subquery_objects.update(sub_resolved.required_objects, sub_resolved.subquery_objects)
    query_columns = [d.name for d in resolved.dimensions]
    refs = [r for _, r in added]

    def named_columns(planned: Select) -> tuple[Select, list[str]]:
        if synthetic:
            planned = _rename_columns(planned, synthetic)
        return _name_per_buckets(planned, query_columns, refs)

    named, columns = named_columns(scan)
    # Also with every ``per`` dimension selected: the groups are then the
    # query's own, and the members are the ones without rows among them.
    if population is ReaggregatePopulation.DIMENSION:
        layout = _population_layout(measure, per, carry, resolved, query, model)
        history = _history(scan, sub_resolved, layout.axis, resolved)
        named = _with_population(
            named,
            None if history is None else named_columns(history)[0],
            measure,
            [entry for entry, _ in added],
            layout,
            resolved,
            query,
            model,
            dialect,
            qualify_table,
        )
    return named, {entry: column for (entry, _), column in zip(added, columns, strict=True)}


_MEMBERS = "reagg_members"
_FACTS = "reagg_facts"
_PERIODS = "reagg_periods"
_HISTORY = "reagg_history"
_GRID = "reagg_grid"
_RUNS = "reagg_runs"
_CARRIED = "reagg_carried"
_SHOWN = "reagg_shown"
_RUN = "__reagg_run"
_MATCHED = "__reagg_matched"
_ROWS_ALL = "reagg_rows_all"
_ROWS = "reagg_rows"


@dataclass(frozen=True)
class _Layout:
    """Where a ``population: dimension`` first stage reads its groups from."""

    #: The data objects the members are listed from.
    members: set[str]
    #: The query's dimensions crossed with the members (an order month).
    periods: list[str]
    #: The periods on the time axis a carried value moves along, coarsest
    #: first; empty when the metric does not carry forward or has no such axis.
    axis: list[str]


def _population_layout(
    measure: str,
    all_per: list[str],
    carry: bool,
    resolved: ResolvedQuery,
    query: QueryObject,
    model: SemanticModel,
) -> _Layout:
    """The members and periods of :func:`_with_population`, refusing a query
    whose dimensions neither describe the members nor read none of them.

    *all_per* is the metric's ``per``. The members are the data objects one of
    its dimensions' objects reaches, if one reaches the others. With *carry*,
    the periods on a date (a grain, or a date or timestamp dimension) are the
    axis a value is carried along; they have to be one date column.
    """
    graph = JoinGraph.of(model, query.use_path_names)
    roots = {view for entry in all_per if (view := model.dimensions[entry].view) is not None}
    members = next(
        (reach for root in sorted(roots) if roots <= (reach := {root} | graph.descendants(root))),
        None,
    )
    if members is None:
        _refuse_population(
            measure,
            f"no data object of {all_per} reaches the others, so there is no one table "
            f"of members to list",
        )
    # A dimension counts with every object its computed column reads: one on
    # the clients computed from the sales is no property of a client without
    # sales, and the member query, which has no sales, could not read it.
    reads = {
        d.name: None if d.via is not None else _column_objects(d.object_name, d.column_name, model)
        for d in resolved.dimensions
    }
    # A query dimension reading no member object is a period to cross with them.
    periods = [
        d.name
        for d in resolved.dimensions
        if d.coalesce_alias is None and (objs := reads[d.name]) is not None and not objs & members
    ]
    reads.update({entry: _field_objects(entry, model) for entry in all_per})
    outside = [
        name
        for name, objs in reads.items()
        if name not in periods and (objs is None or not objs <= members)
    ]
    if outside:
        _refuse_population(
            measure,
            f"each dimension of 'per' has to describe the members of {all_per}, and "
            f"one of the query either them or none of their data objects, read from "
            f"their data objects alone, and {outside} "
            f"{'does' if len(outside) == 1 else 'do'} not",
        )
    dates = [d for d in resolved.dimensions if d.name in periods and _is_date(d, model)]
    if not carry or not dates:
        return _Layout(members, periods, [])
    columns = {(d.object_name, d.column_name) for d in dates}
    if len(columns) > 1:
        raise ResolutionError(
            [
                SemanticError(
                    code="REAGGREGATE_CARRY_FORWARD_AXIS",
                    message=(
                        f"The first stage of '{measure}' carries a member's value forward "
                        f"in time, but the query groups by more than one date: "
                        f"{[d.name for d in dates]}."
                    ),
                    path="select.dimensions",
                    hint="Group by grains of one date only.",
                )
            ]
        )
    # Coarsest first; a dimension with no grain is the column itself, finest.
    axis = sorted(dates, key=lambda d: -_GRAIN_RANK[d.grain] if d.grain else 1)
    return _Layout(members, periods, [d.name for d in axis])


def _is_date(dim: ResolvedDimension, model: SemanticModel) -> bool:
    """Whether *dim* is a point in time: at a grain, or declared as a date."""
    declared = model.dimensions.get(dim.dimension)
    return dim.grain is not None or (
        declared is not None and declared.result_type in DATE_BEARING_TYPES
    )


def _history(
    scan: Select, scan_resolved: ResolvedQuery, axis: list[str], resolved: ResolvedQuery
) -> Select | None:
    """*scan* read past the query's filters on the *axis* date, or ``None``
    when there is nothing to carry or no such filter.

    The filter says which periods to show, not which a value may come from:
    February's balance carries an account's January snapshot when the query
    starts in February, as a cumulative reads the months before its first.
    """
    if not axis:
        return None
    time_dim = next(d for d in resolved.dimensions if d.name == axis[0])
    own = next(
        (
            d.name
            for d in scan_resolved.dimensions
            if (d.object_name, d.column_name) == (time_dim.object_name, time_dim.column_name)
        ),
        None,
    )
    predicates = time_filters(scan_resolved, own) if own is not None else []
    copy = lookback_query(scan, predicates, "_history", scan_resolved) if predicates else None
    if copy is None:
        return None
    added, body = copy
    return replace(body, ctes=[*scan.ctes, *added])


def _with_population(
    facts: Select,
    history: Select | None,
    measure: str,
    per: list[str],
    layout: _Layout,
    resolved: ResolvedQuery,
    query: QueryObject,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
) -> Select:
    """*facts* over every member of the *per* dimensions, not only those with rows.

    *per* are the entries stage 1 added to the query's grouping. The members
    are the query's dimensions and *per*, planned as a query of their own over
    the data objects of *layout* - so it starts from the member table, not
    from a fact, also when the query already groups by that dimension. A query
    dimension among them describes the member (the customer's country); one
    that reads none of them (the order month) is a period, crossed with the
    members, its values the ones *facts* has, so every customer is in every
    month with orders. The part of a filter that tests only the member
    objects applies to the members (see :func:`_member_part`); one on the
    facts limits the measure, and so the periods, not who counts.

    A member without rows takes the measure's ``defaultValue``, or 0 for a
    count, which is what a count reports over no rows; otherwise NULL. On a
    carried *layout* it first takes its latest earlier value (see
    :func:`_carried`), read from *history* when the query filters the date.
    """
    periods = layout.periods
    member_model = model.model_copy(
        update={
            "data_objects": {n: o for n, o in model.data_objects.items() if n in layout.members}
        }
    )
    described = [
        d
        for d in query.select.dimensions
        if not (isinstance(d, str) and DimensionRef.parse(d).name in periods)
    ]
    member_query = QueryObject(
        select=QuerySelect(dimensions=[*described, *per]),
        where=[part for f in query.where if (part := _member_part(f, layout.members, model))],
        use_path_names=list(query.use_path_names),
    )
    # Imported here for the same reason as in :func:`_stage_one`.
    from orionbelt.compiler.pipeline import plan_in_own_right

    listed, _ = plan_in_own_right(member_query, member_model, dialect, qualify_table)
    listed = _own_cte_names(listed, f"{_MEMBERS}_")
    facts = _own_cte_names(_marked(facts), f"{_FACTS}_")
    keys = [*(d.name for d in resolved.dimensions), *per]
    ctes = [
        *listed.ctes,
        CTE(name=_MEMBERS, query=replace(listed, ctes=[])),
        *facts.ctes,
        CTE(name=_FACTS, query=replace(facts, ctes=[])),
    ]
    source = _FACTS
    if history is not None:
        history = _own_cte_names(_marked(history), f"{_HISTORY}_")
        ctes += [*history.ctes, CTE(name=_HISTORY, query=replace(history, ctes=[]))]
        source = _HISTORY
    joins: list[Join | Unnest] = []
    if periods:
        ctes.append(CTE(name=_PERIODS, query=_distinct(source, periods)))
        joins.append(Join(join_type=JoinType.CROSS, source=_PERIODS, alias=_PERIODS))
    member_keys = [k for k in keys if k not in periods]
    on = _join_on(_MEMBERS, source, member_keys) if member_keys else None
    if periods:
        by_period = _join_on(_PERIODS, source, periods)
        on = by_period if on is None else BinaryOp(left=on, op="AND", right=by_period)
    joins.append(Join(join_type=JoinType.LEFT, source=source, alias=source, on=on))
    # The measure where the member has a row, and NULL where it has none: an
    # unmatched LEFT JOIN column is NULL on every engine but ClickHouse, which
    # fills a non-Nullable one with its type's default (0), so the marker the
    # facts carry tells the two apart.
    value: Expr = CaseExpr(
        when_clauses=[
            (
                BinaryOp(
                    left=ColumnRef(name=_MATCHED, table=source), op="=", right=Literal.number(1)
                ),
                ColumnRef(name=measure, table=source),
            )
        ]
    )
    empty = _empty_value(measure, model)
    columns = [
        AliasedExpr(
            expr=ColumnRef(name=k, table=_PERIODS if k in periods else _MEMBERS),
            alias=k,
        )
        for k in keys
    ]
    if not layout.axis:
        if empty is not None:
            value = FunctionCall(name="COALESCE", args=[value, empty])
        return Select(
            columns=[*columns, AliasedExpr(expr=value, alias=measure)],
            from_=From(source=_MEMBERS, alias=_MEMBERS),
            joins=joins,
            ctes=ctes,
        )
    grid = Select(
        columns=[*columns, AliasedExpr(expr=value, alias=measure)],
        from_=From(source=_MEMBERS, alias=_MEMBERS),
        joins=joins,
    )
    ctes.append(CTE(name=_GRID, query=grid))
    if history is not None:
        ctes.append(CTE(name=_SHOWN, query=_distinct(_FACTS, periods)))
    return _carried(
        ctes, keys, measure, layout, empty, shown=history is not None, model=model, dialect=dialect
    )


def _marked(facts: Select) -> Select:
    """*facts* with a constant column, set on every row it has."""
    return replace(
        facts, columns=[*facts.columns, AliasedExpr(expr=Literal.number(1), alias=_MATCHED)]
    )


def _distinct(source: str, columns: list[str]) -> Select:
    """The distinct *columns* of the CTE *source*."""
    return Select(
        columns=[AliasedExpr(expr=ColumnRef(name=c, table=source), alias=c) for c in columns],
        from_=From(source=source, alias=source),
        distinct=True,
    )


def _carried(
    ctes: list[CTE],
    keys: list[str],
    measure: str,
    layout: _Layout,
    empty: Expr | None,
    *,
    shown: bool,
    model: SemanticModel,
    dialect: Dialect,
) -> Select:
    """The grid's values with each member's carried into the periods it has
    no row in, then the members without any as the default.

    A running count of a member's values, ordered along the date, numbers its
    runs: each value starts one, and the periods after it until the next value
    belong to it. The value is the run's only one, so its ``MAX`` over the run
    is the carried value - a portable form of ``LAST_VALUE ... IGNORE NULLS``,
    which Postgres and MySQL do not have. A member's other dimensions and the
    periods off the date (a product) keep their own runs. A value is a row's
    measure: a period whose measure is NULL carries the one before it.

    With *shown*, the grid was read past the query's date filter, and only the
    periods the query has rows in are kept.
    """
    partition = partition_keys([k for k in keys if k not in layout.axis], model, dialect)
    run = WindowFunction(
        func_name="COUNT",
        args=[ColumnRef(name=measure)],
        partition_by=partition,
        order_by=[
            OrderByItem(expr=ColumnRef(name=a), desc=False, nulls_last=True) for a in layout.axis
        ],
        frame=WindowFrame(),
    )
    ctes.append(
        CTE(
            name=_RUNS,
            query=Select(
                columns=[
                    *(AliasedExpr(expr=ColumnRef(name=k), alias=k) for k in keys),
                    AliasedExpr(expr=ColumnRef(name=measure), alias=measure),
                    AliasedExpr(expr=run, alias=_RUN),
                ],
                from_=From(source=_GRID, alias=_GRID),
            ),
        )
    )
    carried: Expr = WindowFunction(
        func_name="MAX",
        args=[ColumnRef(name=measure)],
        partition_by=[*partition, ColumnRef(name=_RUN)],
    )
    if empty is not None:
        carried = FunctionCall(name="COALESCE", args=[carried, empty])
    ctes.append(
        CTE(
            name=_CARRIED,
            query=Select(
                columns=[
                    *(AliasedExpr(expr=ColumnRef(name=k), alias=k) for k in keys),
                    AliasedExpr(expr=carried, alias=measure),
                ],
                from_=From(source=_RUNS, alias=_RUNS),
            ),
        )
    )
    return Select(
        columns=[
            *(AliasedExpr(expr=ColumnRef(name=k, table=_CARRIED), alias=k) for k in keys),
            AliasedExpr(expr=ColumnRef(name=measure, table=_CARRIED), alias=measure),
        ],
        from_=From(source=_CARRIED, alias=_CARRIED),
        joins=(
            [
                Join(
                    join_type=JoinType.INNER,
                    source=_SHOWN,
                    alias=_SHOWN,
                    on=_join_on(_CARRIED, _SHOWN, layout.periods),
                )
            ]
            if shown
            else []
        ),
        ctes=ctes,
    )


def _refuse_population(measure: str, reason: str) -> NoReturn:
    raise ResolutionError(
        [
            SemanticError(
                code="REAGGREGATE_POPULATION_GROUPING",
                message=(
                    f"The first stage of '{measure}' takes its population from the "
                    f"dimension, but {reason}."
                ),
                path="select.dimensions",
                hint=(
                    "Group by dimensions of the members, or use a reaggregate metric "
                    "without 'population: dimension'."
                ),
            )
        ]
    )


def _empty_value(measure: str, model: SemanticModel) -> Expr | None:
    """What *measure* reports for a member without rows (see :func:`_with_population`)."""
    declared = model.effective_measures.get(measure)
    if declared is None:
        return None
    if declared.default_value is not None:
        return Literal(value=declared.default_value)
    if declared.aggregation in (AggregationType.COUNT, AggregationType.COUNT_DISTINCT):
        return Literal.number(0)
    return None


def _member_part(
    item: QueryFilter | QueryFilterGroup, objects: set[str], model: SemanticModel
) -> QueryFilter | QueryFilterGroup | None:
    """The part of *item* that tests only columns on *objects*, or ``None``.

    The conjuncts of an ``and`` group stand alone, so the ones on the members
    apply to them whatever the others test: ``Gender = F and Order Date >=
    2022`` keeps ``Gender = F``. An ``or`` or negated group is all or nothing,
    since dropping part of it changes what the rest means. An ``exists`` /
    ``nonexists`` test applies only when its subquery reads *objects* only too.
    A computed column counts with every object its expression reads.
    """
    if isinstance(item, QueryFilterGroup):
        if item.logic is FilterLogic.AND and not item.negated:
            parts = [p for child in item.filters if (p := _member_part(child, objects, model))]
            return item.model_copy(update={"filters": parts}) if parts else None
        whole = all(_member_part(child, objects, model) == child for child in item.filters)
        return item if whole else None
    reads = _filter_objects(item, model)
    return item if reads is not None and reads <= objects else None


def _filter_objects(item: QueryFilter, model: SemanticModel) -> set[str] | None:
    """Every data object *item* reads, or ``None`` when it reads through a role.

    A field's own object and whatever its computed column reads; for an
    ``exists`` / ``nonexists``, also the subquery's object and each of its
    filters' fields, resolved as the subquery resolves them (a column of its
    object, a dimension, or a qualified ``Object.Column``).
    """
    found = _field_objects(item.field, model)
    if found is None:
        return None
    if item.subquery is not None:
        inner = item.subquery
        found.add(inner.data_object)
        for sub in inner.filter:
            ref = _resolve_subquery_filter_field(sub.field, model, inner.data_object, [])
            if ref is None:
                return None
            found |= _column_objects(*ref, model)
            if sub.subquery is not None:
                nested = _filter_objects(sub, model)
                if nested is None:
                    return None
                found |= nested
    return found


def _field_objects(field: str, model: SemanticModel) -> set[str] | None:
    """The data objects a dimension or ``Object.Column`` *field* reads."""
    column = _field_column(field, model)
    return None if column is None else _column_objects(*column, model)


def _field_column(field: str, model: SemanticModel) -> tuple[str, str] | None:
    """The (data object, column) a query filter's *field* tests."""
    dimension = model.dimensions.get(field.split(":", 1)[0])
    if dimension is not None:
        if dimension.via is not None or dimension.view is None or dimension.column is None:
            return None
        return dimension.view, dimension.column
    if "." in field:
        obj, _, col = field.partition(".")
        return obj.strip(), col.strip()
    return None


def _column_objects(obj: str, column: str, model: SemanticModel) -> set[str]:
    """*obj* and the data objects its *column*'s expression reads."""
    return {obj, *model.column_reference_objects(obj, column)}


def _cumulative_time_dimension(measure: str, model: SemanticModel) -> str | None:
    """The time dimension of the cumulative metric *measure*'s chain reaches,
    through reaggregate metrics, or ``None`` when it reaches none."""
    seen: set[str] = set()
    current: str | None = measure
    while current is not None and current not in seen:
        seen.add(current)
        metric = model.metrics.get(current)
        if metric is None:
            return None
        if metric.type is MetricType.CUMULATIVE:
            return metric.time_dimension
        if metric.type is not MetricType.REAGGREGATE:
            return None
        current = metric.measure
    return None


def _rank(grain: TimeGrain | None) -> int:
    """A grain's place from finest to coarsest; no grain is the column itself."""
    return _GRAIN_RANK[grain] if grain is not None else -1


def _distinct_axes(
    measure: str,
    dims: list[str | CoalesceDimension],
    model: SemanticModel,
) -> tuple[SemanticModel, list[str | CoalesceDimension], dict[str, str]]:
    """Stage 1's model and dimensions, with a metric's axes kept apart.

    A dimension named twice at different grains - the query's
    ``Order Date:year`` beside ``per: [Order Date]``, two ``per`` buckets of
    it, or one an outer reaggregate metric passes down - gives two columns of
    one name, which the planner names after the dimension. For a plain measure
    :func:`_name_per_buckets` tells them apart after planning, but a metric
    planned by a pass of its own reads them by name while planning: a
    cumulative's look-back joined the shown months by year and multiplied the
    rows, and a nested reaggregate metric's stages joined and grouped the
    months by year.

    So when *measure* is a reaggregate metric or its chain reaches a
    cumulative one, each dimension named more than once keeps its name for one
    entry - the first, or for the cumulative's own time dimension, which the
    cumulative has to find selected, the one at the finest grain, so its
    window orders the rows stage 1 has without ties - and every other entry is
    planned as a synthetic dimension at its grain. The returned mapping
    renames those columns back after planning, and the kept one when it is
    not the first, each to the column the query names it by
    (``Order Date:month`` for the second of two grains, see
    :func:`column_name`); a ``per`` column left with the plain name
    :func:`_name_per_buckets` then tells apart by position as for any measure.
    """
    time_dim = _cumulative_time_dimension(measure, model)
    metric = model.metrics.get(measure)
    if time_dim is None and (metric is None or metric.type is not MetricType.REAGGREGATE):
        return model, list(dims), {}
    refs = [
        (index, reaggregate_per_ref(model, entry))
        for index, entry in enumerate(dims)
        if isinstance(entry, str)
    ]
    by_name: dict[str, list[tuple[int, DimensionRef]]] = {}
    for index, ref in refs:
        by_name.setdefault(ref.name, []).append((index, ref))
    renamed: set[int] = set()
    for name, entries in by_name.items():
        if len(entries) < 2 or name not in model.dimensions:
            continue
        declared = model.dimensions[name].time_grain
        keep = entries[0][0]
        if name == time_dim:
            # The finest of them: stage 1 has a row per bucket of it, and a
            # coarser key would leave those rows tied in the running window
            # (per day and month, ordered by month, the order depended on how
            # the rows arrived). A bucket with no grain is the column itself.
            keep = min(entries, key=lambda e: _rank(e[1].grain or declared))[0]
        renamed.update(i for i, _ in entries if i != keep)
    if not renamed:
        return model, list(dims), {}
    dimensions = dict(model.dimensions)
    planned: list[str | CoalesceDimension] = list(dims)
    synthetic: dict[str, str] = {}
    earlier: list[tuple[str, TimeGrain | None]] = []
    for index, ref in refs:
        # The column the query names this entry by, as resolution names it.
        column = column_name(ref.name, ref.grain, earlier)
        earlier.append((ref.name, ref.grain))
        if index not in renamed:
            # The entry that keeps its name is planned under it, the only one
            # by that name; selected after another grain, its column is still
            # the suffixed one. One per name, and renamed in one pass with the
            # synthetic ones, so it cannot take another entry's column.
            if column != ref.name:
                synthetic[ref.name] = column
            continue
        declared_dim = model.dimensions[ref.name]
        grain = ref.grain or declared_dim.time_grain
        name = f"__reaggregate_axis_{index}_{ref.name}"
        dimensions[name] = declared_dim.model_copy(update={"name": name, "time_grain": grain})
        planned[index] = name
        synthetic[name] = column
    return model.model_copy(update={"dimensions": dimensions}), planned, synthetic


def _rename_columns(scan: Select, names: dict[str, str]) -> Select:
    """*scan* with its projected columns aliased per *names* renamed."""
    columns = [
        AliasedExpr(expr=c.expr, alias=names[c.alias])
        if isinstance(c, AliasedExpr) and c.alias in names
        else c
        for c in scan.columns
    ]
    return replace(scan, columns=columns)


def _name_per_buckets(
    scan: Select, query_dims: list[str], refs: list[DimensionRef]
) -> tuple[Select, list[str]]:
    """Give each ``per`` bucket of a dimension named twice a column of its own.

    The planner names a dimension's column after the dimension at any grain, so
    ``Order Date:day`` beside the query's ``Order Date:month``, or beside a
    second ``per`` bucket of that date, would leave two columns of one name,
    which most engines reject in a CTE and the second stage could not tell
    apart. Such a ``per`` column becomes ``name:grain``, as the planner names
    one itself when it sees both (see :func:`grain_column`); a column it
    named so is kept. The query's own columns come first in the scan and keep
    their names. Returns the scan and each ref's column name.
    """
    names = [*query_dims, *(r.name for r in refs)]
    pending = {name: [r for r in refs if r.name == name] for name in names if names.count(name) > 1}
    columns_of = [r.name for r in refs]
    aliases = {_alias(c) for c in scan.columns}
    for index, ref in enumerate(refs):
        named = grain_column(ref.name, ref.grain)
        if named in aliases and named not in query_dims and ref in pending.get(ref.name, []):
            pending[ref.name].remove(ref)
            columns_of[index] = named
    if not pending:
        return scan, columns_of
    query_columns = Counter(query_dims)
    columns: list[Expr] = []
    for col in scan.columns:
        alias = _alias(col)
        if isinstance(col, AliasedExpr) and alias in pending and pending[alias]:
            if query_columns[alias]:
                query_columns[alias] -= 1
            else:
                ref = pending[alias].pop(0)
                col = AliasedExpr(expr=col.expr, alias=grain_column(ref.name, ref.grain))
                columns_of[refs.index(ref)] = col.alias
        columns.append(col)
    if any(pending.values()):
        raise RuntimeError(f"reaggregate stage 1 lost a 'per' column: {pending}")
    return replace(scan, columns=columns), columns_of


def _stage_two(
    inner: str,
    metrics: list[ResolvedMeasure],
    dim_names: list[str],
    per_columns: dict[str, str],
    model: SemanticModel,
    dialect: Dialect,
) -> Select:
    """Each metric's aggregate over the stage-1 values, grouped by the query grain.

    *per_columns* names the stage-1 column of each ``per`` entry stage 1 added.
    """
    columns: list[Expr] = [
        AliasedExpr(expr=ColumnRef(name=d, table=inner), alias=d) for d in dim_names
    ]
    for m in metrics:
        assert m.reaggregate_measure is not None and m.reaggregate_aggregation is not None
        arg = ColumnRef(name=m.reaggregate_measure, table=inner)
        metric = model.metrics.get(m.name)
        exact: tuple[Expr, OBMLType] | None = None
        target: OBMLType | None
        if metric is not None and m.reaggregate_aggregation is ReaggregateAggType.AVG:
            integer_values = reaggregated_values_are_integers(model, m.reaggregate_measure)
            exact = exact_reaggregate_avg(metric, integer_values, model.settings, dialect, arg)
        if exact is not None:
            expr, target = exact
        else:
            fraction = None
            if metric is not None and metric.percentile is not None:
                fraction = Decimal(str(metric.percentile))
            expr = FunctionCall(
                name=_FUNCTIONS[m.reaggregate_aggregation],
                args=[arg],
                fraction=fraction,
                order_by=_time_order(m, metric, per_columns, inner, model, arg),
            )
            target = resolve_metric_data_type(metric, model.settings) if metric else None
        if target is not None:
            expr = dialect.cast_to_obml_type(expr, target)
        columns.append(AliasedExpr(expr=expr, alias=m.name))
    return Select(
        columns=columns,
        from_=From(source=inner, alias=inner),
        group_by=[ColumnRef(name=d, table=inner) for d in dim_names],
    )


def _time_order(
    m: ResolvedMeasure,
    metric: Metric | None,
    per_columns: dict[str, str],
    inner: str,
    model: SemanticModel,
    value: Expr,
) -> list[OrderByItem]:
    """The ordering of a ``first`` / ``last`` second stage: by its time ``per`` entry.

    A ``per`` bucket the query's grouping covers is not a stage-1 column, and
    then each group holds one value, which any order picks: the value orders
    itself.
    """
    aggregation = m.reaggregate_aggregation
    if metric is None or aggregation not in (ReaggregateAggType.FIRST, ReaggregateAggType.LAST):
        return []
    entries = time_per_entries(metric.per, model.dimensions)
    column = per_columns.get(entries[0]) if entries else None
    key: Expr = ColumnRef(name=column, table=inner) if column else value
    return [OrderByItem(expr=key, desc=aggregation is ReaggregateAggType.LAST)]


def _own_cte_names(scan: Select, prefix: str) -> Select:
    """*scan* with each of its CTEs, nested ones too, named under *prefix*.

    The first stage is planned as a query of its own, so its wrappers name
    their CTEs as the outer query's do (``base``, ``main``), and it is nested
    inside the outer query. Snowflake resolves a nested ``FROM "base"`` to the
    outer query's CTE of that name, which has none of the first stage's
    columns. Unique names leave no reference to resolve the wrong way.

    Each name is *prefix*, a number unique within the scan, and the CTE's own
    name without the prefix an inner stage gave it: ``reagg_1_inner_2_base``.
    Stacking the prefixes instead grew a name by one per nested stage, past
    PostgreSQL's 63 bytes, which truncates two siblings to the same name.
    """
    return _numbered_ctes(scan, prefix, count(1))


def _numbered_ctes(scan: Select, prefix: str, numbers: Iterator[int]) -> Select:
    renamed: dict[str, str] = {}
    ctes: list[CTE] = []
    for cte in scan.ctes:
        name = f"{prefix}{next(numbers)}_{_STAGE_CTE_PREFIX.sub('', cte.name)}"
        own = cte.query
        if isinstance(own, Select):
            own = _numbered_ctes(own, prefix, numbers)
        ctes.append(CTE(name=name, query=rename_cte_sources(own, renamed)))
        renamed[cte.name] = name
    renamed_scan = rename_cte_sources(replace(scan, ctes=ctes), renamed)
    assert isinstance(renamed_scan, Select)
    return renamed_scan


def _hoist_ctes(ctes: list[CTE], captured: list[CTE]) -> None:
    """Add the CTEs the captured plan reads that the final query lacks.

    The planner's CTEs (a multi-fact plan's legs) usually survive every wrapper
    unchanged, and are then shared rather than repeated.
    """
    present = {cte.name: cte for cte in ctes}
    for cte in captured:
        if cte.name not in present:
            ctes.append(cte)
        elif present[cte.name] != cte:
            raise RuntimeError(f"CTE '{cte.name}' was rewritten after the components were taken")


def _join_on(left: str, right: str, dim_names: list[str]) -> Expr:
    on: Expr | None = None
    for d in dim_names:
        part = null_safe_eq(ColumnRef(name=d, table=left), ColumnRef(name=d, table=right))
        on = part if on is None else BinaryOp(left=on, op="AND", right=part)
    assert on is not None
    return on


def _split_metrics(
    resolved: ResolvedQuery,
) -> tuple[dict[str, list[ResolvedMeasure]], dict[str, ResolvedMeasure]]:
    """Each selected measure's leaf components, and the derived metrics over a
    reaggregate metric - those this pass rebuilds from their components."""
    leaves = {
        m.name: metric_leaf_components(m, resolved.metric_components) for m in resolved.measures
    }
    split = {
        m.name: m
        for m in resolved.measures
        if not m.is_reaggregate
        and any(c.is_reaggregate for c in leaves[m.name])
        # Finished by the late window pass, a placeholder until then.
        and not is_late(m, resolved.metric_components)
    }
    return leaves, split


def _late_placeholder() -> Expr:
    """A NULL standing in for a value the late window pass computes.

    A constant, not an aggregate: it is rebuilt from the resolution by wrappers
    whose outer query is not grouped (totals, a period-over-period comparison),
    where ``MAX(NULL)`` beside a dimension is an error. The query stays one row
    per group all the same, since the reaggregate metric it reads is always
    projected, and that placeholder is an aggregate (``_placeholder_value``).
    """
    return Cast(expr=Literal(value=None), type_name="float")


def _plain_components(
    leaves: dict[str, list[ResolvedMeasure]], split: dict[str, ResolvedMeasure]
) -> dict[str, tuple[str, ResolvedMeasure]]:
    """The rebuilt metrics' other components, each under a private alias."""
    components: dict[str, tuple[str, ResolvedMeasure]] = {}
    for metric_name in split:
        for comp in leaves[metric_name]:
            if not comp.is_reaggregate and comp.name not in components:
                components[comp.name] = (f"_reagg_component_{len(components) + 1}", comp)
    return components


def _plan_value(resolved: ResolvedQuery, comp: ResolvedMeasure) -> Expr:
    """What the plan computes for *comp* at the query grain: a plain
    component's bare aggregate, a reaggregate metric's placeholder."""
    if comp.is_reaggregate:
        return comp.expression
    return resolved.projected_expressions.get(comp.name, comp.expression)


def _placeholder_value(m: ResolvedMeasure, model: SemanticModel, dialect: Dialect) -> Expr:
    """``MAX`` of a NULL of the type the metric's finished value has.

    The placeholder is discarded, but every wrapper in between projects it, and
    a formula over it (``{[Named Customers]} * 2``) has to bind: the inner
    measure's own aggregate can be a string, which a NULL of the metric's type
    is not. It is an aggregate, as the column it stands for is: a query left
    with nothing else to aggregate - a filterContext moves its measure into a
    CTE of its own - stays one row without dimensions, also over no rows,
    rather than one per fact row.

    A ``min`` or ``max`` over a ``min`` or ``max`` measure declares no type
    anywhere, and an untyped NULL is text on Postgres, which ``* 2`` will not
    bind to. It takes the inner measure's source column type, as CFL's NULL
    pads do.
    """
    target = reaggregated_data_type(model, m.name)
    base = model.reaggregated_measure(m.reaggregate_measure or "")
    null: Expr = Literal(value=None)
    if target is not None:
        null = dialect.cast_to_obml_type(null, target)
    elif base is not None:
        null = Cast(expr=null, type_name=_source_type(base[1], model))
    return FunctionCall(name="MAX", args=[null])


def _source_type(measure: Measure, model: SemanticModel) -> str:
    """The abstract type of a single-column measure's column, else its
    declared ``resultType``."""
    if len(measure.columns) == 1:
        ref = measure.columns[0]
        obj = model.data_objects.get(ref.view) if ref.view else None
        if obj is not None and ref.column in obj.columns:
            return obj.columns[ref.column].abstract_type.value
    return measure.result_type.value


def capture_reaggregate_components(
    ast: Select, resolved: ResolvedQuery, model: SemanticModel, dialect: Dialect
) -> Select:
    """Prepare the plan for wrappers that run before the reaggregate pass.

    Each reaggregate metric's placeholder becomes an aggregate NULL of the
    metric's type, in the resolution and in the plan's columns. The planner's
    form was the measure's name as a bare column, which only bound by
    accident, and every wrapper that rebuilds a projection from the resolution
    (period-over-period does) carried it into its own CTE. The reaggregate pass
    discards the value.

    The plain components of derived metrics over a reaggregate metric are kept
    in ``reaggregate_components``: each is the bare aggregate the planner
    inlines into the formula, taken here from the plan it was planned in,
    because the reaggregate pass runs after every other wrapper, whose FROM no
    longer reaches the fact tables. Each gets a column of its own, never the
    selected measure's, which is cast to the measure's type, so reading it
    would make the metric depend on what else is selected.
    """
    late = [
        m
        for m in [*resolved.measures, *resolved.metric_components.values()]
        if is_late(m, resolved.metric_components)
    ]
    for m in [*resolved.measures, *resolved.metric_components.values()]:
        if m.is_reaggregate:
            m.expression = _placeholder_value(m, model, dialect)
    # A window over a reaggregate metric, and a formula reading one, is finished
    # by the late window pass; until then every pass reads a placeholder.
    for m in late:
        if m.deferred_expression is None:
            m.deferred_expression = m.expression
            m.expression = _late_placeholder()

    leaves, split = _split_metrics(resolved)
    components = _plain_components(leaves, split)
    if components:
        dim_names = {d.name for d in resolved.dimensions}
        columns: list[Expr] = [c for c in ast.columns if _alias(c) in dim_names]
        columns += [
            AliasedExpr(expr=_plan_value(resolved, comp), alias=alias)
            for alias, comp in components.values()
        ]
        resolved.reaggregate_components = replace(
            ast, columns=columns, having=None, order_by=[], limit=None, offset=None
        )

    reaggregates = {m.name: m for m in resolved.measures if m.is_reaggregate}

    late_names = {m.name for m in late}

    def placeholder(col: Expr) -> Expr:
        alias = _alias(col)
        if alias in reaggregates:
            return AliasedExpr(expr=reaggregates[alias].expression, alias=alias)
        if alias in late_names:
            # A window over a reaggregate metric, or a formula reading one: the
            # late window pass computes it from finished columns.
            return AliasedExpr(expr=_late_placeholder(), alias=alias)
        if alias not in split:
            return col
        by_name = {c.name: c for c in leaves[alias]}
        expr = metric_over_components(
            split[alias],
            resolved.metric_components,
            lambda name: _plan_value(resolved, by_name[name]),
            model,
            dialect,
        )
        return AliasedExpr(expr=expr, alias=alias)

    return replace(ast, columns=[placeholder(c) for c in ast.columns])


def wrap_with_reaggregate(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    query: QueryObject,
) -> Select:
    """Replace each reaggregate placeholder with its two-stage value.

    Runs after every other wrapper, so *ast* is the finished query with a
    placeholder column per reaggregate metric, which the wrappers carried like
    any measure. That query becomes ``reagg_base`` without those columns.

    A derived metric over a reaggregate metric is rebuilt here too. The planner
    inlined the placeholder into the metric's one column, so the column is
    dropped from ``reagg_base`` and the formula is re-expanded in the outer
    query over each component: a reaggregate one from its stage-2 CTE, a plain
    one from ``reagg_components`` (see :func:`capture_reaggregate_components`).
    """
    leaves, split = _split_metrics(resolved)
    reaggregates = [m for m in resolved.measures if m.is_reaggregate]
    for metric_name in split:
        for comp in leaves[metric_name]:
            if comp.is_reaggregate and comp.name not in {r.name for r in reaggregates}:
                reaggregates.append(comp)
    if not reaggregates:
        return ast
    names = {m.name for m in reaggregates}
    dim_names = [d.name for d in resolved.dimensions]

    # One pair of CTEs per (measure, per, having): two metrics that differ only
    # in their second-stage function share the scan.
    groups: dict[
        tuple[str, tuple[str, ...], str, ReaggregatePopulation, bool], list[ResolvedMeasure]
    ]
    groups = {}
    for m in reaggregates:
        assert m.reaggregate_measure is not None
        having = _having(model, m.name)
        key = (
            m.reaggregate_measure,
            tuple(m.reaggregate_per),
            _having_key(having),
            _population(model, m.name),
            _carry_forward(model, m.name),
        )
        groups.setdefault(key, []).append(m)

    ctes = list(ast.ctes)
    # A window over a reaggregate metric, or a formula reading one, is finished
    # by the late window pass; here it is a constant in the outer projection.
    # Kept in the base, it was the base's only column for a query selecting
    # nothing else, which then scanned the fact rows ungrouped: one result row
    # per source row rather than the single one, and none over no rows.
    late = {m.name for m in resolved.measures if is_late(m, resolved.metric_components)}
    base_columns = [c for c in ast.columns if _alias(c) not in names | split.keys() | late]
    # With no dimensions and nothing else selected the base projects nothing;
    # each stage-2 CTE is then the one row the query returns.
    keep_base = bool(base_columns)
    # Every member counts in a population metric, so its groups are rows of
    # the query too: by account and month, every account in every month, also
    # one without a snapshot. The rows are those and the base's, and the base
    # marks its own, where a row only the members have reads the empty value.
    by_rows = bool(
        dim_names
        and keep_base
        and any(population is ReaggregatePopulation.DIMENSION for _, _, _, population, _ in groups)
    )
    if keep_base:
        if by_rows:
            base_columns = [*base_columns, AliasedExpr(expr=Literal.number(1), alias=_MATCHED)]
        ctes.append(
            CTE(
                name=_BASE,
                query=replace(
                    ast, columns=base_columns, order_by=[], limit=None, offset=None, ctes=[]
                ),
            )
        )

    component_alias = {name: alias for name, (alias, _) in _plain_components(leaves, split).items()}
    joined: list[str] = []
    if component_alias:
        captured = resolved.reaggregate_components
        assert captured is not None, "the components pass runs before this one"
        _hoist_ctes(ctes, captured.ctes)
        ctes.append(CTE(name=_COMPONENTS, query=replace(captured, ctes=[])))
        joined.append(_COMPONENTS)

    # A count with no stage-2 row for a row of the query counted no group: a
    # ``having`` removed them all, or the row is one only the members have.
    counted_after_having = {
        m.name
        for m in reaggregates
        if m.reaggregate_aggregation is ReaggregateAggType.COUNT
        and (by_rows or _having(model, m.name))
    }
    source_of: dict[str, str] = {}
    stage_two_names: list[str] = []
    grid_names: list[str] = []
    for idx, ((measure, per, _, population, carry), metrics) in enumerate(groups.items(), start=1):
        inner_name = f"reagg_{idx}_inner"
        outer_name = f"reagg_{idx}"
        having = _having(model, metrics[0].name)
        scan, per_columns = _stage_one(
            measure,
            list(per),
            having,
            population,
            carry,
            resolved,
            query,
            model,
            dialect,
            qualify_table,
        )
        ctes.append(CTE(name=inner_name, query=_own_cte_names(scan, f"{inner_name}_")))
        stage_two = _stage_two(inner_name, metrics, dim_names, per_columns, model, dialect)
        ctes.append(CTE(name=outer_name, query=stage_two))
        stage_two_names.append(outer_name)
        if population is ReaggregatePopulation.DIMENSION:
            grid_names.append(outer_name)
        for m in metrics:
            source_of[m.name] = outer_name

    anchor = _BASE if keep_base else stage_two_names[0]
    if by_rows:
        ctes.append(
            CTE(
                name=_ROWS_ALL,
                query=UnionAll(
                    queries=[_distinct(name, dim_names) for name in [_BASE, *grid_names]]
                ),
            )
        )
        ctes.append(CTE(name=_ROWS, query=_distinct(_ROWS_ALL, dim_names)))
        anchor = _ROWS
    joins: list[Join | Unnest] = []
    for name in [*([_BASE] if by_rows else []), *stage_two_names, *joined]:
        if name == anchor:
            continue
        joins.append(
            Join(
                join_type=JoinType.LEFT if dim_names else JoinType.CROSS,
                source=name,
                alias=name,
                on=_join_on(anchor, name, dim_names) if dim_names else None,
            )
        )

    def stage_two_value(name: str) -> Expr:
        value = ColumnRef(name=name, table=source_of[name])
        if name in counted_after_having:
            # A group whose stage-1 rows ``having`` all removed has no stage-2
            # row; the join leaves NULL where the count is 0. The 0 takes the
            # count's own cast, so a declared ``dataType`` holds on both sides.
            zero: Expr = Literal.number(0)
            target = resolve_metric_data_type(model.metrics[name], model.settings)
            if target is not None:
                zero = dialect.cast_to_obml_type(zero, target)
            return FunctionCall(name="COALESCE", args=[value, zero])
        return value

    def empty_of(name: str) -> Expr:
        """What *name* reports over no rows: a measure its defaultValue, 0 for
        a count, a derived metric its formula over those, otherwise NULL."""
        empty = _empty_value(name, model)
        if empty is not None:
            return empty
        metric = next((m for m in resolved.measures if m.name == name), None)
        if metric is not None and metric.expression is not None and metric.component_measures:
            return metric_over_components(
                metric, resolved.metric_components, empty_of, model, dialect
            )
        return Literal.null()

    def on_base_row(value: Expr, name: str) -> Expr:
        """*value*, read from the base or a CTE with its rows, where the base
        has the row; :func:`empty_of` where only the members do. Tested on
        the base's marker, not on *value*: ClickHouse reads an unmatched
        non-Nullable column as 0."""
        if not by_rows:
            return value
        matched = BinaryOp(
            left=ColumnRef(name=_MATCHED, table=_BASE), op="=", right=Literal.number(1)
        )
        return CaseExpr(when_clauses=[(matched, value)], else_clause=empty_of(name))

    def read(alias: str) -> Expr:
        if alias in late:
            return _late_placeholder()
        if alias in split:
            # Assembled in the outer projection, so it is read by its alias.
            return ColumnRef(name=alias)
        if alias in source_of:
            return stage_two_value(alias)
        if alias in dim_names:
            return ColumnRef(name=alias, table=anchor)
        return on_base_row(ColumnRef(name=alias, table=anchor if not by_rows else _BASE), alias)

    columns: list[Expr] = []
    for col in ast.columns:
        alias = _alias(col)
        assert alias is not None, "planner columns are always aliased"
        if alias in split:
            value: Expr = metric_over_components(
                split[alias],
                resolved.metric_components,
                lambda name: (
                    on_base_row(ColumnRef(name=component_alias[name], table=_COMPONENTS), name)
                    if name in component_alias
                    else stage_two_value(name)
                ),
                model,
                dialect,
            )
        else:
            value = read(alias)
        columns.append(AliasedExpr(expr=value, alias=alias))

    # The ordering keys come back as bare aliases; both sides of each join
    # carry the dimension columns, so each key names the CTE it reads.
    order_by = [
        OrderByItem(
            expr=read(item.expr.name)
            if isinstance(item.expr, ColumnRef) and item.expr.table is None
            else item.expr,
            desc=item.desc,
            nulls_last=item.nulls_last,
        )
        for item in outer_order_by(resolved, model)
    ]

    return Select(
        columns=columns,
        from_=From(source=anchor, alias=anchor),
        joins=joins,
        order_by=order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=ctes,
    )
