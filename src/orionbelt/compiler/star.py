"""Star schema planner: single fact table with dimension joins → AST."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    Select,
)
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery, make_column_expr
from orionbelt.compiler.type_resolver import resolve_measure_data_type, resolve_metric_data_type
from orionbelt.models.query import NullsPosition
from orionbelt.models.semantic import DataObject, SemanticModel

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect


_GROUPING_FLAG_PREFIX = "_g_"


def _nulls_last(nulls: NullsPosition | None) -> bool | None:
    """Map a QueryOrderBy.nulls value to the AST's ``nulls_last`` flag.

    ``None`` keeps the dialect default; explicit ``FIRST`` / ``LAST``
    forces the corresponding ``NULLS FIRST`` / ``NULLS LAST`` clause.
    """
    if nulls is None:
        return None
    return nulls == NullsPosition.LAST


def _grouping_flag_alias(dim_alias: str) -> str:
    """Build the GROUPING() flag column alias for a dimension.

    Per PLAN_with_rollup.md §"Output: GROUPING() flag columns" — convention is
    ``_g_<dim>`` with a stable prefix so callers can filter on detail vs
    subtotal vs grand-total rows.
    """
    return f"{_GROUPING_FLAG_PREFIX}{dim_alias}"


def _substitute_measure_refs(
    expr: Expr,
    components: dict[str, ResolvedMeasure],
) -> Expr:
    """Walk a metric AST tree and replace ColumnRef placeholders with aggregate expressions."""
    if isinstance(expr, ColumnRef) and expr.table is None and expr.name in components:
        return components[expr.name].expression
    if isinstance(expr, BinaryOp):
        new_left = _substitute_measure_refs(expr.left, components)
        new_right = _substitute_measure_refs(expr.right, components)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    return expr


def _expand_measure_refs(expr: Expr, measure_exprs: dict[str, Expr]) -> Expr:
    """Replace bare ColumnRef aliases in HAVING with their full aggregate expressions."""
    if isinstance(expr, ColumnRef) and expr.table is None and expr.name in measure_exprs:
        return measure_exprs[expr.name]
    if isinstance(expr, BinaryOp):
        new_left = _expand_measure_refs(expr.left, measure_exprs)
        new_right = _expand_measure_refs(expr.right, measure_exprs)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    return expr


@dataclass
class CflLegInfo:
    """Information about a single CFL leg for explain output."""

    measure_source: str
    common_root: str
    reason: str
    measures: list[str]
    joins: list[str]


@dataclass
class QueryPlan:
    """A planned query ready for AST construction."""

    ast: Select
    cfl_legs: list[CflLegInfo] = field(default_factory=list)


class StarSchemaPlanner:
    """Plans star-schema queries: single fact base with dimension joins."""

    def plan(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
        dialect: Dialect | None = None,
    ) -> QueryPlan:
        builder = QueryBuilder()
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        base_object = model.data_objects.get(resolved.base_object)
        if not base_object:
            return QueryPlan(ast=builder.build())

        base_alias = resolved.base_object

        # SELECT: dimensions (apply time grain truncation if specified)
        grouping_dim_aliases: list[str] = []
        for dim in resolved.dimensions:
            col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
            if dim.grain and dialect:
                col = dialect.render_time_grain(col, dim.grain)
            builder.select(AliasedExpr(expr=col, alias=dim.name))
            if resolved.grouping is not None:
                grouping_dim_aliases.append(dim.name)

        # SELECT: measures (aggregated) — for metrics, substitute component refs
        settings = model.settings
        measure_exprs: dict[str, Expr] = {}
        for measure in resolved.measures:
            if measure.component_measures:
                expr: Expr = _substitute_measure_refs(
                    measure.expression, resolved.metric_components
                )
                metric = model.metrics.get(measure.name)
                if metric and dialect:
                    resolved_type = resolve_metric_data_type(metric, settings)
                    if resolved_type:
                        expr = dialect.cast_to_obml_type(expr, resolved_type)
                builder.select(AliasedExpr(expr=expr, alias=measure.name))
            else:
                expr = measure.expression
                model_measure = model.measures.get(measure.name)
                if model_measure and dialect:
                    resolved_type = resolve_measure_data_type(model_measure, settings)
                    if resolved_type:
                        expr = dialect.cast_to_obml_type(expr, resolved_type)
                builder.select(AliasedExpr(expr=expr, alias=measure.name))
            measure_exprs[measure.name] = expr

        # FROM: base fact table
        builder.from_(qualify(base_object), alias=base_alias)

        # JOINs: dimension and intermediate tables
        joined = {base_alias}
        for step in resolved.join_steps:
            # Determine which side of the step needs to be joined
            if step.to_object not in joined:
                new_object = step.to_object
            elif step.from_object not in joined:
                new_object = step.from_object
            else:
                continue  # both already joined
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

        # GROUP BY (all dimension columns, with time grain if applicable).
        # Stash the per-dim group-by expression by alias so GROUPING() below
        # can reuse the SAME expression — Postgres rejects GROUPING(<alias>)
        # with "column does not exist" and requires the group-key expression.
        group_by_exprs: dict[str, Expr] = {}
        for dim in resolved.dimensions:
            gb_col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
            if dim.grain and dialect:
                gb_col = dialect.render_time_grain(gb_col, dim.grain)
            builder.group_by(gb_col)
            group_by_exprs[dim.name] = gb_col

        # GROUPING() flag columns + grouping modifier (rollup/cube)
        if resolved.grouping is not None and grouping_dim_aliases:
            builder.grouping(resolved.grouping.value)
            for alias in grouping_dim_aliases:
                gb_arg = group_by_exprs.get(alias) or ColumnRef(name=alias)
                flag_col = FunctionCall(name="GROUPING", args=[gb_arg])
                builder.select(AliasedExpr(expr=flag_col, alias=_grouping_flag_alias(alias)))

        # HAVING — expand alias references to actual CAST'd aggregate expressions
        for hf in resolved.having_filters:
            builder.having(_expand_measure_refs(hf.expression, measure_exprs))

        # ORDER BY (use alias for time-grained dimensions)
        grained_cols: dict[tuple[str, str | None], str] = {
            (d.source_column, d.object_name): d.name for d in resolved.dimensions if d.grain
        }
        for expr, desc, nulls in resolved.order_by_exprs:
            if isinstance(expr, ColumnRef) and (expr.name, expr.table) in grained_cols:
                expr = ColumnRef(name=grained_cols[(expr.name, expr.table)])
            builder.order_by(expr, desc=desc, nulls_last=_nulls_last(nulls))

        # LIMIT / OFFSET
        if resolved.limit is not None:
            builder.limit(resolved.limit)
        if resolved.offset is not None:
            builder.offset(resolved.offset)

        return QueryPlan(ast=builder.build())
