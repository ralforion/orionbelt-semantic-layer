"""Raw-mode planner: project physical columns without aggregation."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import AliasedExpr, ColumnRef
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.resolution import ResolvedQuery
from orionbelt.compiler.star import QueryPlan
from orionbelt.models.semantic import DataObject, SemanticModel

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect


class RawPlanner:
    """Plans raw-mode queries: flat projection of physical columns.

    Emits ``SELECT [DISTINCT] field1, field2, ... FROM base [LEFT JOIN ...]
    [WHERE ...] [ORDER BY ...] [LIMIT n]`` with no GROUP BY and no aggregates.
    Multi-fact queries (fields spanning independent fact tables) are rejected
    by the pipeline; raw CFL is a planned follow-up.
    """

    def plan(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
        dialect: Dialect | None = None,  # noqa: ARG002 — kept for parity with other planners
    ) -> QueryPlan:
        builder = QueryBuilder()
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        base_object = model.data_objects.get(resolved.base_object)
        if not base_object:
            return QueryPlan(ast=builder.build())

        base_alias = resolved.base_object

        # SELECT — one aliased ColumnRef per field, in declaration order.
        for f in resolved.fields:
            builder.select(
                AliasedExpr(
                    expr=ColumnRef(name=f.source_column, table=f.object_name),
                    alias=f.alias,
                )
            )

        if resolved.distinct:
            builder.distinct(True)

        # FROM
        builder.from_(qualify(base_object), alias=base_alias)

        # JOINs (same logic as star schema; raw mode reuses join_steps).
        joined = {base_alias}
        for step in resolved.join_steps:
            if step.to_object not in joined:
                new_object = step.to_object
            elif step.from_object not in joined:
                new_object = step.from_object
            else:
                continue
            obj = model.data_objects.get(new_object)
            if not obj:
                continue
            on_expr = graph.build_join_condition(step)
            builder.join(
                table=qualify(obj),
                on=on_expr,
                join_type=step.join_type,
                alias=new_object,
            )
            joined.add(new_object)

        # WHERE
        for wf in resolved.where_filters:
            builder.where(wf.expression)

        # ORDER BY
        for expr, desc in resolved.order_by_exprs:
            builder.order_by(expr, desc=desc)

        # LIMIT / OFFSET
        if resolved.limit is not None:
            builder.limit(resolved.limit)
        if resolved.offset is not None:
            builder.offset(resolved.offset)

        return QueryPlan(ast=builder.build())
