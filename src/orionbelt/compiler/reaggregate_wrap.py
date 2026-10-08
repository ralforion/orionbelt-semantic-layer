"""Two-stage aggregation for ``MetricType.REAGGREGATE``.

A reaggregate metric is its measure computed at the query grain plus the
``per`` dimensions (stage 1), then aggregated again to the query grain
(stage 2) - "average revenue per customer, by country" is ``SUM(revenue)`` per
country and customer, then ``AVG`` of those per country.

The planner has projected the measure's aggregate under the metric's name at
the query grain, which is a placeholder and not the metric. This wrapper moves
the planner's query into a ``reagg_base`` CTE without that column, and per
distinct (measure, ``per``) pair adds:

- ``reagg_<n>_inner``: the measure planned *as a query in its own right* at the
  finer grain, so its base object, join path and fanout check are derived for
  that grain (the same route ``filter_wrap`` takes for a filterContext scan);
- ``reagg_<n>``: the second-stage aggregate of that scan, grouped by the query
  dimensions.

The outer query reads ``reagg_base`` and LEFT JOINs each ``reagg_<n>`` on the
query dimensions, NULL-safely, since a NULL dimension value is a group of its
own. Both stages see the query's WHERE, so every group of ``reagg_base`` has
rows in stage 1 and the join finds it.
"""

from __future__ import annotations

from collections.abc import Callable

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Join,
    JoinType,
    OrderByItem,
    Select,
    Unnest,
)
from orionbelt.compiler.metric_expansion import metric_leaf_components, metric_over_components
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.time_lookback import null_safe_eq
from orionbelt.compiler.type_resolver import (
    exact_reaggregate_avg,
    measure_yields_integers,
    resolve_metric_data_type,
)
from orionbelt.dialect.base import Dialect
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import DataObject, ReaggregateAggType, SemanticModel
from orionbelt.models.types import OBMLType

_BASE = "reagg_base"

_FUNCTIONS: dict[ReaggregateAggType, str] = {
    ReaggregateAggType.SUM: "SUM",
    ReaggregateAggType.AVG: "AVG",
    ReaggregateAggType.MIN: "MIN",
    ReaggregateAggType.MAX: "MAX",
    ReaggregateAggType.COUNT: "COUNT",
}


def _alias(expr: Expr) -> str | None:
    return expr.alias if isinstance(expr, AliasedExpr) else None


def _stage_one(
    measure: str,
    per: list[str],
    resolved: ResolvedQuery,
    query: QueryObject,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
) -> Select:
    """The measure at the query grain plus *per*, planned as a query of its own.

    Only the query's row filters carry over: its HAVING, ORDER BY and LIMIT
    describe the final result, not this scan.
    """
    in_query = {d.name for d in resolved.dimensions}
    sub_query = QueryObject(
        select=QuerySelect(
            dimensions=[*query.select.dimensions, *(p for p in per if p not in in_query)],
            measures=[measure],
        ),
        where=list(query.where),
        use_path_names=list(query.use_path_names),
        allow_fan_out=query.allow_fan_out,
    )
    # Imported here: the pipeline owns the phase order, and importing it at
    # module scope would be a cycle back through ``compiler.passes``.
    from orionbelt.compiler.pipeline import plan_in_own_right

    scan, sub_resolved = plan_in_own_right(sub_query, model, dialect, qualify_table)
    # Its warnings are about this query - a fan trap in the scan is one here.
    resolved.warnings.extend(sub_resolved.warnings)
    # So are its tables: a ``per`` dimension can join one the query does not,
    # and the result cache has to see it change.
    resolved.subquery_objects.update(sub_resolved.required_objects, sub_resolved.subquery_objects)
    return scan


def _stage_two(
    inner: str,
    metrics: list[ResolvedMeasure],
    dim_names: list[str],
    model: SemanticModel,
    dialect: Dialect,
) -> Select:
    """Each metric's aggregate over the stage-1 values, grouped by the query grain."""
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
            base = model.effective_measures.get(m.reaggregate_measure)
            integer_values = base is not None and measure_yields_integers(
                base, model.settings, model
            )
            exact = exact_reaggregate_avg(metric, integer_values, model.settings, dialect, arg)
        if exact is not None:
            expr, target = exact
        else:
            expr = FunctionCall(name=_FUNCTIONS[m.reaggregate_aggregation], args=[arg])
            target = resolve_metric_data_type(metric, model.settings) if metric else None
        if target is not None:
            expr = dialect.cast_to_obml_type(expr, target)
        columns.append(AliasedExpr(expr=expr, alias=m.name))
    return Select(
        columns=columns,
        from_=From(source=inner, alias=inner),
        group_by=[ColumnRef(name=d, table=inner) for d in dim_names],
    )


def _join_on(left: str, right: str, dim_names: list[str]) -> Expr:
    on: Expr | None = None
    for d in dim_names:
        part = null_safe_eq(ColumnRef(name=d, table=left), ColumnRef(name=d, table=right))
        on = part if on is None else BinaryOp(left=on, op="AND", right=part)
    assert on is not None
    return on


def wrap_with_reaggregate(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    query: QueryObject,
) -> Select:
    """Replace each reaggregate placeholder with its two-stage value.

    A derived metric over a reaggregate metric is rebuilt here too. The planner
    inlined the placeholder into the metric's one column, so the column is
    dropped from ``reagg_base``, the metric's other components are projected
    there in their own right, and the formula is re-expanded in the outer query
    over each component's column - the route ``filter_wrap`` takes for a metric
    over a filter-contexted measure.
    """
    leaves = {
        m.name: metric_leaf_components(m, resolved.metric_components) for m in resolved.measures
    }
    split = {
        m.name: m
        for m in resolved.measures
        if not m.is_reaggregate and any(c.is_reaggregate for c in leaves[m.name])
    }
    reaggregates = [m for m in resolved.measures if m.is_reaggregate]
    for metric_name in split:
        for comp in leaves[metric_name]:
            if comp.is_reaggregate and comp.name not in {r.name for r in reaggregates}:
                reaggregates.append(comp)
    if not reaggregates:
        return ast
    names = {m.name for m in reaggregates}
    dim_names = [d.name for d in resolved.dimensions]

    # One pair of CTEs per (measure, per): two metrics that differ only in their
    # second-stage function share the scan.
    groups: dict[tuple[str, tuple[str, ...]], list[ResolvedMeasure]] = {}
    for m in reaggregates:
        assert m.reaggregate_measure is not None
        groups.setdefault((m.reaggregate_measure, tuple(m.reaggregate_per)), []).append(m)

    ctes = list(ast.ctes)
    base_columns = [c for c in ast.columns if _alias(c) not in names | split.keys()]
    # A rebuilt metric's plain components, which its dropped column carried.
    projected = {a for c in base_columns if (a := _alias(c)) is not None}
    for metric_name in split:
        for comp in leaves[metric_name]:
            if comp.is_reaggregate or comp.name in projected:
                continue
            base_columns.append(
                AliasedExpr(
                    expr=resolved.projected_expressions.get(comp.name, comp.expression),
                    alias=comp.name,
                )
            )
            projected.add(comp.name)
    # With no dimensions and nothing else selected the base projects nothing;
    # each stage-2 CTE is then the one row the query returns.
    keep_base = bool(base_columns)
    if keep_base:
        ctes.append(
            CTE(
                name=_BASE,
                query=Select(
                    columns=base_columns,
                    from_=ast.from_,
                    joins=ast.joins,
                    where=ast.where,
                    group_by=ast.group_by,
                    having=ast.having,
                    grouping=ast.grouping,
                ),
            )
        )

    source_of: dict[str, str] = {}
    stage_two_names: list[str] = []
    for idx, ((measure, per), metrics) in enumerate(groups.items(), start=1):
        inner_name = f"reagg_{idx}_inner"
        outer_name = f"reagg_{idx}"
        ctes.append(
            CTE(
                name=inner_name,
                query=_stage_one(
                    measure, list(per), resolved, query, model, dialect, qualify_table
                ),
            )
        )
        ctes.append(
            CTE(name=outer_name, query=_stage_two(inner_name, metrics, dim_names, model, dialect))
        )
        stage_two_names.append(outer_name)
        for m in metrics:
            source_of[m.name] = outer_name

    anchor = _BASE if keep_base else stage_two_names[0]
    joins: list[Join | Unnest] = []
    for name in stage_two_names:
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

    def read(alias: str) -> ColumnRef:
        if alias in split:
            # Assembled in the outer projection, so it is read by its alias.
            return ColumnRef(name=alias)
        return ColumnRef(name=alias, table=source_of.get(alias, anchor))

    columns: list[Expr] = []
    for col in ast.columns:
        alias = _alias(col)
        assert alias is not None, "planner columns are always aliased"
        if alias in split:
            value: Expr = metric_over_components(
                split[alias],
                resolved.metric_components,
                lambda name: ColumnRef(name=name, table=source_of.get(name, anchor)),
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
