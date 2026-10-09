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
from dataclasses import replace
from itertools import count

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
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
    Unnest,
)
from orionbelt.compiler.metric_expansion import metric_leaf_components, metric_over_components
from orionbelt.compiler.metric_resolution import reaggregate_per_covered, reaggregate_per_ref
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import ResolutionError, ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.time_lookback import null_safe_eq, rename_cte_sources
from orionbelt.compiler.type_resolver import (
    exact_reaggregate_avg,
    reaggregated_data_type,
    reaggregated_values_are_integers,
    resolve_metric_data_type,
)
from orionbelt.dialect.base import Dialect
from orionbelt.models.query import DimensionRef, QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import (
    DataObject,
    Measure,
    ReaggregateAggType,
    ReaggregateHaving,
    SemanticModel,
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
}


#: The prefix ``_own_cte_names`` gives a stage-1 CTE, which a further stage
#: replaces rather than adds to.
_STAGE_CTE_PREFIX = re.compile(r"^reagg_\d+_inner_\d+_")


def _having(model: SemanticModel, name: str) -> list[ReaggregateHaving]:
    """The reaggregate metric *name*'s conditions on its first stage."""
    metric = model.metrics.get(name)
    return metric.having if metric is not None else []


def _having_key(having: list[ReaggregateHaving]) -> str:
    return "\n".join(h.model_dump_json() for h in having)


def _alias(expr: Expr) -> str | None:
    return expr.alias if isinstance(expr, AliasedExpr) else None


def _stage_one(
    measure: str,
    per: list[str],
    having: list[ReaggregateHaving],
    resolved: ResolvedQuery,
    query: QueryObject,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
) -> Select:
    """The measure at the query grain plus *per*, planned as a query of its own.

    Only the query's row filters carry over: its HAVING, ORDER BY and LIMIT
    describe the final result, not this scan. The scan's HAVING is the
    metric's own *having*.

    A ``per`` entry the query's grouping already covers is left out, and so is
    one naming the same bucket as an earlier entry.
    """
    added: list[tuple[str, DimensionRef]] = []
    for entry in per:
        ref = reaggregate_per_ref(model, entry)
        if reaggregate_per_covered(model, ref, resolved.dimensions) or any(
            ref == other for _, other in added
        ):
            continue
        added.append((entry, ref))
    sub_query = QueryObject(
        select=QuerySelect(
            dimensions=[*query.select.dimensions, *(entry for entry, _ in added)],
            measures=[measure],
        ),
        where=list(query.where),
        having=[QueryFilter(field=h.field, op=h.op, value=h.value) for h in having],
        use_path_names=list(query.use_path_names),
        allow_fan_out=query.allow_fan_out,
    )
    # Imported here: the pipeline owns the phase order, and importing it at
    # module scope would be a cycle back through ``compiler.passes``.
    from orionbelt.compiler.pipeline import plan_in_own_right

    try:
        scan, sub_resolved = plan_in_own_right(sub_query, model, dialect, qualify_table)
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
    return _name_per_buckets(scan, [d.name for d in resolved.dimensions], [r for _, r in added])


def _name_per_buckets(scan: Select, query_dims: list[str], refs: list[DimensionRef]) -> Select:
    """Give each ``per`` bucket of a dimension named twice a column of its own.

    The planner names a dimension's column after the dimension at any grain, so
    ``Order Date:day`` beside the query's ``Order Date:month``, or beside a
    second ``per`` bucket of that date, would leave two columns of one name,
    which most engines reject in a CTE and the second stage could not tell
    apart. Such a ``per`` column becomes ``name:grain``. The query's own
    columns come first in the scan and keep their names.
    """
    names = [*query_dims, *(r.name for r in refs)]
    pending = {name: [r for r in refs if r.name == name] for name in names if names.count(name) > 1}
    if not pending:
        return scan
    query_columns = Counter(query_dims)
    columns: list[Expr] = []
    for col in scan.columns:
        alias = _alias(col)
        if isinstance(col, AliasedExpr) and alias in pending and pending[alias]:
            if query_columns[alias]:
                query_columns[alias] -= 1
            else:
                ref = pending[alias].pop(0)
                grain = ref.grain.value if ref.grain else "value"
                col = AliasedExpr(expr=col.expr, alias=f"{ref.name}:{grain}")
        columns.append(col)
    if any(pending.values()):
        raise RuntimeError(f"reaggregate stage 1 lost a 'per' column: {pending}")
    return replace(scan, columns=columns)


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
            integer_values = reaggregated_values_are_integers(model, m.reaggregate_measure)
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
        if not m.is_reaggregate and any(c.is_reaggregate for c in leaves[m.name])
    }
    return leaves, split


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
    for m in [*resolved.measures, *resolved.metric_components.values()]:
        if m.is_reaggregate:
            m.expression = _placeholder_value(m, model, dialect)

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

    def placeholder(col: Expr) -> Expr:
        alias = _alias(col)
        if alias in reaggregates:
            return AliasedExpr(expr=reaggregates[alias].expression, alias=alias)
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
    groups: dict[tuple[str, tuple[str, ...], str], list[ResolvedMeasure]] = {}
    for m in reaggregates:
        assert m.reaggregate_measure is not None
        having = _having(model, m.name)
        key = (m.reaggregate_measure, tuple(m.reaggregate_per), _having_key(having))
        groups.setdefault(key, []).append(m)

    ctes = list(ast.ctes)
    base_columns = [c for c in ast.columns if _alias(c) not in names | split.keys()]
    # With no dimensions and nothing else selected the base projects nothing;
    # each stage-2 CTE is then the one row the query returns.
    keep_base = bool(base_columns)
    if keep_base:
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

    counted_after_having = {
        m.name
        for m in reaggregates
        if m.reaggregate_aggregation is ReaggregateAggType.COUNT and _having(model, m.name)
    }
    source_of: dict[str, str] = {}
    stage_two_names: list[str] = []
    for idx, ((measure, per, _), metrics) in enumerate(groups.items(), start=1):
        inner_name = f"reagg_{idx}_inner"
        outer_name = f"reagg_{idx}"
        having = _having(model, metrics[0].name)
        scan = _stage_one(
            measure, list(per), having, resolved, query, model, dialect, qualify_table
        )
        ctes.append(CTE(name=inner_name, query=_own_cte_names(scan, f"{inner_name}_")))
        ctes.append(
            CTE(name=outer_name, query=_stage_two(inner_name, metrics, dim_names, model, dialect))
        )
        stage_two_names.append(outer_name)
        for m in metrics:
            source_of[m.name] = outer_name

    anchor = _BASE if keep_base else stage_two_names[0]
    joins: list[Join | Unnest] = []
    for name in [*stage_two_names, *joined]:
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
            # row; the join leaves NULL where the count is 0.
            return FunctionCall(name="COALESCE", args=[value, Literal.number(0)])
        return value

    def read(alias: str) -> Expr:
        if alias in split:
            # Assembled in the outer projection, so it is read by its alias.
            return ColumnRef(name=alias)
        if alias in source_of:
            return stage_two_value(alias)
        return ColumnRef(name=alias, table=anchor)

    columns: list[Expr] = []
    for col in ast.columns:
        alias = _alias(col)
        assert alias is not None, "planner columns are always aliased"
        if alias in split:
            value: Expr = metric_over_components(
                split[alias],
                resolved.metric_components,
                lambda name: (
                    ColumnRef(name=component_alias[name], table=_COMPONENTS)
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
