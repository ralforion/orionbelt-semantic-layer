"""CFL dimensionsExclude planning: EXCEPT-based anti-join group.

Extracted from ``cfl.py`` as module-level functions whose first argument is
the :class:`~orionbelt.compiler.cfl.CFLPlanner` instance (``planner``). The
planner keeps thin delegators so the public surface is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    ColumnRef,
    Except,
    Expr,
    Join,
    JoinType,
    Select,
)
from orionbelt.compiler import cfl_projection
from orionbelt.compiler.filter_resolution import conjuncts
from orionbelt.compiler.filters import static_filter_unreachable_error
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.resolution import (
    ResolutionError,
    ResolvedDimension,
    ResolvedFilter,
    ResolvedQuery,
    make_dimension_expr,
)
from orionbelt.compiler.star import QueryPlan, _nulls_last
from orionbelt.models.semantic import (
    DataObject,
    SemanticModel,
)

if TYPE_CHECKING:
    from orionbelt.compiler.cfl import CFLPlanner
    from orionbelt.dialect.base import Dialect


def plan_dimensions_exclude(
    planner: CFLPlanner,
    resolved: ResolvedQuery,
    model: SemanticModel,
    qualify_table: Callable[[DataObject], str] | None = None,
    dialect: Dialect | None = None,
) -> QueryPlan:
    """Plan a dimensionsExclude query using EXCEPT pattern.

    Generates:
      WITH dim_group_00 AS (SELECT DISTINCT dims FROM ...),
           dim_group_01 AS (...),
           non_combinations AS (
             SELECT ... FROM dim_group_00 CROSS JOIN dim_group_01
             EXCEPT
             SELECT ... FROM fact_joins
           )
      SELECT ... FROM non_combinations ORDER BY ... LIMIT ...
    """
    graph = JoinGraph.of(model, use_path_names=resolved.use_path_names or None)

    def qualify(obj: DataObject) -> str:
        return qualify_table(obj) if qualify_table else obj.qualified_code

    # Partition dimensions into independent groups
    dim_groups = planner._partition_dimensions(resolved, graph)

    # The candidate sets are scans of their own: the existing pairs carry the
    # query's filters, but a candidate read past an ``enforce: always`` filter
    # comes back as a "missing" pair from rows the filter excludes.
    enforced = [wf for wf in resolved.where_filters if wf.enforced]

    ctes: list[CTE] = []

    # CTE per dimension group: SELECT DISTINCT via GROUP BY
    group_cte_names: list[str] = []
    for i, group_dims in enumerate(dim_groups):
        cte_name = f"dim_group_{i:02d}"
        group_cte_names.append(cte_name)
        cte_query = planner._build_group_distinct_select(
            group_dims,
            model,
            graph,
            qualify,
            via_constraints=resolved.via_constraints or None,
            dialect=dialect,
            enforced=enforced,
        )
        ctes.append(CTE(name=cte_name, query=cte_query))

    # Build "all_pairs": CROSS JOIN of all dim_group CTEs
    all_pairs_builder = QueryBuilder()
    for dim in resolved.dimensions:
        all_pairs_builder.select(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
    all_pairs_builder.from_(group_cte_names[0], alias=group_cte_names[0])
    for cte_name in group_cte_names[1:]:
        all_pairs_builder._joins.append(
            Join(join_type=JoinType.CROSS, source=cte_name, alias=cte_name)
        )
    all_pairs_select = all_pairs_builder.build()

    # EXCEPT CTE: all_pairs EXCEPT existing_pairs, the actual combinations via
    # fact-table joins. Where several roots join the dimensions, a combination
    # any of them holds exists, so each root's are taken away in turn.
    roots: list[str | None] = [*resolved.dimension_roots] or [None]
    remaining = all_pairs_select
    for i, root in enumerate(roots):
        existing_pairs_select = planner._build_existing_pairs_select(
            resolved, model, graph, qualify, dialect, root
        )
        cte_name = "non_combinations" if i == len(roots) - 1 else f"non_combinations_{i:02d}"
        ctes.append(CTE(name=cte_name, query=Except(left=remaining, right=existing_pairs_select)))
        remaining_builder = QueryBuilder()
        for dim in resolved.dimensions:
            remaining_builder.select(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
        remaining = remaining_builder.from_(cte_name, alias=cte_name).build()

    # Outer query: SELECT from non_combinations with ORDER BY / LIMIT
    outer_builder = QueryBuilder()
    for dim in resolved.dimensions:
        outer_builder.select(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
    outer_builder.from_("non_combinations", alias="non_combinations")

    for expr, desc, nulls in resolved.order_by_exprs:
        outer_builder.order_by(
            planner._remap_cfl_order_by(expr, resolved, model),
            desc=desc,
            nulls_last=_nulls_last(nulls),
        )
    if resolved.limit is not None:
        outer_builder.limit(resolved.limit)
    if resolved.offset is not None:
        outer_builder.offset(resolved.offset)

    outer = outer_builder.build()
    final = Select(
        columns=outer.columns,
        from_=outer.from_,
        joins=outer.joins,
        order_by=outer.order_by,
        limit=outer.limit,
        offset=outer.offset,
        ctes=ctes,
    )
    return QueryPlan(ast=final)


def partition_dimensions(
    resolved: ResolvedQuery,
    graph: JoinGraph,
) -> list[list[ResolvedDimension]]:
    """Partition dimensions into groups on independent branches."""
    obj_to_dims: dict[str, list[ResolvedDimension]] = {}
    for dim in resolved.dimensions:
        obj_to_dims.setdefault(dim.object_name, []).append(dim)

    # Cluster: two objects are in the same group if one is a descendant
    # of the other (i.e., connected via directed join paths).
    objects = sorted(obj_to_dims.keys())
    groups: list[set[str]] = []
    assigned: set[str] = set()

    for obj in objects:
        if obj in assigned:
            continue
        group = {obj}
        reachable = graph.descendants_without_unnest(obj) | {obj}
        for other in objects:
            if (
                other != obj
                and other not in assigned
                and (
                    other in reachable or obj in (graph.descendants_without_unnest(other) | {other})
                )
            ):
                group.add(other)
        groups.append(group)
        assigned.update(group)

    # Convert to lists of ResolvedDimension
    result: list[list[ResolvedDimension]] = []
    for group_objs in groups:
        group_dims: list[ResolvedDimension] = []
        for obj in sorted(group_objs):
            group_dims.extend(obj_to_dims[obj])
        result.append(group_dims)
    return result


def build_group_distinct_select(
    dims: list[ResolvedDimension],
    model: SemanticModel,
    graph: JoinGraph,
    qualify: Callable[[DataObject], str],
    via_constraints: dict[str, str] | None = None,
    dialect: Dialect | None = None,
    enforced: Sequence[ResolvedFilter] = (),
) -> Select:
    """Build SELECT DISTINCT (via GROUP BY) for a group of dimensions.

    Each *enforced* static filter is joined and applied; one the group's scan
    cannot reach refuses the query (``STATIC_FILTER_UNREACHABLE``).
    """
    required_objects = {d.object_name for d in dims}

    # Find the common root that can reach all objects in this group
    if len(required_objects) > 1:
        root = graph.find_common_root(required_objects)
    else:
        root = next(iter(required_objects))

    # If root is a pure dimension table with no joins, check if a fact
    # table can reach it (needed for bridge-table traversal).
    root_obj = model.data_objects.get(root)
    if root_obj and not root_obj.joins and root not in required_objects:
        root = next(iter(sorted(required_objects)))
        root_obj = model.data_objects.get(root)

    builder = QueryBuilder()
    for dim in dims:
        # Through ``make_dimension_expr``, so the candidate combinations are at
        # the grain the query asked for. Reading the raw column made this side
        # of the EXCEPT a set of *day* pairs under a column labelled by the
        # month, and the anti-join then answered a question nobody asked (#369).
        col: Expr = make_dimension_expr(model, dim, dialect)
        builder.select(AliasedExpr(expr=col, alias=dim.name))
        builder.group_by(col)

    if root_obj:
        builder.from_(qualify(root_obj), alias=root)

    reachable = graph.descendants_without_unnest(root) | {root}
    filter_objects: set[str] = set()
    for wf in enforced:
        objects: set[str] = set()
        cfl_projection.collect_table_refs(wf.expression, objects)
        cfl_projection.collect_correlated_tables(wf.expression, objects)
        missing = objects - reachable
        if missing:
            unreachable = ", ".join(sorted(missing))
            names = ", ".join(f"'{d.name}'" for d in dims)
            raise ResolutionError(
                [
                    static_filter_unreachable_error(
                        f"static filter on '{unreachable}'",
                        unreachable,
                        f"the candidate values of {names}",
                    )
                ]
            )
        filter_objects |= objects
        builder.where(wf.expression)

    # Join to reach all dimension objects (and enforced filter objects) from root
    all_needed = required_objects | filter_objects | {root}
    if len(all_needed) > 1:
        steps = graph.find_join_path(
            {root},
            all_needed,
            via_constraints=via_constraints,
        )
        joined_aliases: set[str] = {root}
        for step in steps:
            if step.to_object in joined_aliases:
                continue
            target_obj = model.data_objects.get(step.to_object)
            if target_obj:
                on_expr = graph.build_join_condition(step)
                builder.join(
                    table=qualify(target_obj),
                    on=on_expr,
                    join_type=step.join_type,
                    alias=step.to_object,
                )
                joined_aliases.add(step.to_object)

    return builder.build()


def build_existing_pairs_select(
    planner: CFLPlanner,
    resolved: ResolvedQuery,
    model: SemanticModel,
    graph: JoinGraph,
    qualify: Callable[[DataObject], str],
    dialect: Dialect | None = None,
    root: str | None = None,
) -> Select:
    """Build SELECT for existing dimension combinations via fact-table joins.

    Uses a fact/bridge table as the base and joins through hub tables
    to reach all dimension objects on both branches. *root* is one of the
    query's ``dimension_roots``: the combinations that one holds.
    """
    all_dim_objects = {d.object_name for d in resolved.dimensions}

    # Find fact tables that connect the dimension groups
    fact_tables = (
        {root} if root else set(planner._group_dimensions_into_legs(resolved, model).keys())
    )

    # Use a fact table as the base (pick the one with most joins)
    best_fact = max(
        sorted(fact_tables),
        key=lambda f: len(model.data_objects[f].joins) if f in model.data_objects else 0,
    )
    best_fact_obj = model.data_objects.get(best_fact)

    builder = QueryBuilder()
    for dim in resolved.dimensions:
        # The same grain as the candidate side, or the EXCEPT compares values
        # that were never meant to match.
        col: Expr = make_dimension_expr(model, dim, dialect)
        builder.select(AliasedExpr(expr=col, alias=dim.name))
        builder.group_by(col)

    if best_fact_obj:
        builder.from_(qualify(best_fact_obj), alias=best_fact)

    # Required: all dimension objects + all fact tables. With a root, the
    # objects its filters read too, where it reaches them; one it cannot
    # reach is another root's (below).
    filter_objects: set[str] = set()
    if root:
        for wf in resolved.where_filters:
            cfl_projection.collect_table_refs(wf.expression, filter_objects)
            cfl_projection.collect_correlated_tables(wf.expression, filter_objects)
        filter_objects &= graph.descendants_without_unnest(root) | {root}
    all_needed = all_dim_objects | fact_tables | {best_fact} | filter_objects
    joined: set[str] = {best_fact}
    steps = graph.find_join_path(
        {best_fact},
        all_needed,
        via_constraints=resolved.via_constraints or None,
    )
    for step in steps:
        # Determine the actual new table to join.
        # For reversed edges, to_object may already be joined and the
        # actual new table is from_object.
        if step.to_object not in joined:
            new_table = step.to_object
        elif step.from_object not in joined:
            new_table = step.from_object
        else:
            continue  # Both already joined

        target_obj = model.data_objects.get(new_table)
        if target_obj:
            on_expr = graph.build_join_condition(step)
            builder.join(
                table=qualify(target_obj),
                on=on_expr,
                join_type=step.join_type,
                alias=new_table,
            )
            joined.add(new_table)

    # Apply WHERE filters to existing pairs. A root's combinations take a
    # condition only on objects it joins: a static filter on another root's
    # fact is skipped, as in that root's union leg, and refused if enforced.
    for wf in resolved.where_filters:
        if root is None:
            builder.where(wf.expression)
            continue
        for condition in conjuncts(wf.expression):
            objects: set[str] = set()
            cfl_projection.collect_table_refs(condition, objects)
            cfl_projection.collect_correlated_tables(condition, objects)
            if objects <= joined:
                builder.where(condition)
            elif wf.enforced:
                unreachable = ", ".join(sorted(objects - joined))
                raise ResolutionError(
                    [
                        static_filter_unreachable_error(
                            f"static filter on '{unreachable}'",
                            unreachable,
                            f"the combinations of '{root}'",
                        )
                    ]
                )

    return builder.build()
