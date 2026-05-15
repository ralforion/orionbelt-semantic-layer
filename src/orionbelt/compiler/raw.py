"""Raw-mode planner: project physical columns without aggregation.

Two strategies:

* Single-fact: all field source objects reachable from one base via directed
  joins → flat ``SELECT [DISTINCT] ... FROM base LEFT JOIN ... WHERE ...``.
* Multi-fact (raw CFL): fields span independent facts → one UNION ALL leg
  per leg-root with NULL-padding for fields not reachable from that leg.
  Outer wrapper: ``SELECT [DISTINCT] * FROM (composite) ORDER BY ... LIMIT ...``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    Cast,
    ColumnRef,
    Expr,
    Literal,
    Select,
    UnionAll,
)
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.resolution import ResolvedField, ResolvedQuery, make_column_expr
from orionbelt.compiler.star import CflLegInfo, QueryPlan, _nulls_last
from orionbelt.models.semantic import DataObject, SemanticModel

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect


class RawPlanner:
    """Plans raw-mode queries: flat projection of physical columns.

    Routes to single-fact or CFL strategy based on ``resolved.requires_cfl``.
    """

    def plan(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
        dialect: Dialect | None = None,  # noqa: ARG002 — kept for parity with other planners
        union_by_name: bool = False,
    ) -> QueryPlan:
        if resolved.requires_cfl:
            return self._plan_cfl(resolved, model, qualify_table, union_by_name=union_by_name)
        return self._plan_single_fact(resolved, model, qualify_table)

    # ------------------------------------------------------------------
    # Single-fact path
    # ------------------------------------------------------------------

    def _plan_single_fact(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None,
    ) -> QueryPlan:
        builder = QueryBuilder()
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        base_object = model.data_objects.get(resolved.base_object)
        if not base_object:
            return QueryPlan(ast=builder.build())

        base_alias = resolved.base_object

        for f in resolved.fields:
            builder.select(
                AliasedExpr(
                    expr=make_column_expr(model, f.object_name, f.column_name),
                    alias=f.alias,
                )
            )

        if resolved.distinct:
            builder.distinct(True)

        builder.from_(qualify(base_object), alias=base_alias)

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

        for wf in resolved.where_filters:
            builder.where(wf.expression)

        for expr, desc, nulls in resolved.order_by_exprs:
            builder.order_by(expr, desc=desc, nulls_last=_nulls_last(nulls))

        if resolved.limit is not None:
            builder.limit(resolved.limit)
        if resolved.offset is not None:
            builder.offset(resolved.offset)

        return QueryPlan(ast=builder.build())

    # ------------------------------------------------------------------
    # Multi-fact (raw CFL) path
    # ------------------------------------------------------------------

    def _plan_cfl(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None,
        *,
        union_by_name: bool = False,
    ) -> QueryPlan:
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        # Field source objects are the candidate set. A "leg root" is one
        # that is not a directed descendant of any other source — i.e. it
        # cannot be reached from another field's source via m:1 joins.
        field_objects = {f.object_name for f in resolved.fields}
        leg_roots = self._identify_leg_roots(field_objects, graph)

        # Filter-referenced objects: each leg must include them so WHERE
        # predicates compile. Use the same collector as aggregate CFL.
        filter_objects: set[str] = set()
        for wf in resolved.where_filters:
            self._collect_table_refs(wf.expression, filter_objects)

        union_legs: list[Select] = []
        leg_infos: list[CflLegInfo] = []

        for root in sorted(leg_roots):
            reachable = graph.descendants(root) | {root}
            leg_required = {f.object_name for f in resolved.fields if f.object_name in reachable}
            leg_required.add(root)
            leg_required.update(filter_objects & reachable)

            leg = self._build_cfl_leg(
                root,
                leg_required,
                resolved,
                model,
                graph,
                qualify,
                union_by_name=union_by_name,
            )
            union_legs.append(leg)

            steps = graph.find_join_path({root}, leg_required) if len(leg_required) > 1 else []
            null_strategy = (
                "non-reachable fields omitted (UNION ALL BY NAME fills them)"
                if union_by_name
                else "non-reachable fields NULL-padded"
            )
            leg_infos.append(
                CflLegInfo(
                    measure_source=root,
                    common_root=root,
                    reason=(
                        f'"{root}" is a leg root — fields from this fact + '
                        f"reachable dim objects projected; {null_strategy}"
                    ),
                    measures=[],
                    joins=[f"{s.from_object} → {s.to_object}" for s in steps],
                )
            )

        # Build the composite CTE and the outer wrapper.
        cte_name = "composite_raw_01"
        union_cte = CTE(name=cte_name, query=UnionAll(queries=union_legs))

        outer = QueryBuilder()
        # Re-emit fields by alias from the CTE
        for f in resolved.fields:
            outer.select(AliasedExpr(expr=ColumnRef(name=f.alias), alias=f.alias))
        if resolved.distinct:
            outer.distinct(True)
        outer.from_(cte_name, alias=cte_name)

        # ORDER BY remapped to alias-only refs (CTE has no table qualifier).
        for expr, desc, nulls in resolved.order_by_exprs:
            outer.order_by(
                self._remap_order_by(expr, resolved, model),
                desc=desc,
                nulls_last=_nulls_last(nulls),
            )

        if resolved.limit is not None:
            outer.limit(resolved.limit)
        if resolved.offset is not None:
            outer.offset(resolved.offset)

        outer_select = outer.build()
        final = Select(
            columns=outer_select.columns,
            from_=outer_select.from_,
            joins=outer_select.joins,
            where=outer_select.where,
            group_by=outer_select.group_by,
            having=outer_select.having,
            order_by=outer_select.order_by,
            limit=outer_select.limit,
            offset=outer_select.offset,
            ctes=[union_cte],
            distinct=outer_select.distinct,
        )
        return QueryPlan(ast=final, cfl_legs=leg_infos)

    # ------------------------------------------------------------------
    # CFL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _identify_leg_roots(field_objects: set[str], graph: JoinGraph) -> set[str]:
        """Maximal elements under directed reachability.

        A source is a leg root iff no other source can reach it via directed
        m:1 joins. With strict m:1 joins, leg roots are the "deepest fact"
        nodes — they have descendants but no field-set ancestors.
        """
        roots = set(field_objects)
        for src in field_objects:
            for other in field_objects:
                if other != src and src in graph.descendants(other):
                    roots.discard(src)
                    break
        return roots

    def _build_cfl_leg(
        self,
        root: str,
        leg_required: set[str],
        resolved: ResolvedQuery,
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
        *,
        union_by_name: bool = False,
    ) -> Select:
        """Construct a single UNION ALL leg rooted at *root*.

        When *union_by_name* is True (DuckDB, Snowflake) the leg only emits
        the fields it actually has — the database fills missing columns with
        NULL automatically via ``UNION ALL BY NAME``. Otherwise each leg
        emits a typed ``CAST(NULL AS <type>)`` for non-reachable fields so
        positional UNION ALL line-up is unambiguous.
        """
        builder = QueryBuilder()

        for f in resolved.fields:
            if f.object_name in leg_required:
                builder.select(
                    AliasedExpr(
                        expr=make_column_expr(model, f.object_name, f.column_name),
                        alias=f.alias,
                    )
                )
            elif not union_by_name:
                builder.select(
                    AliasedExpr(
                        expr=self._null_cast_for_field(f, model),
                        alias=f.alias,
                    )
                )

        # FROM the leg root
        root_obj = model.data_objects.get(root)
        if root_obj is not None:
            builder.from_(qualify(root_obj), alias=root)

        # JOINs to all other required objects in this leg. Track every object
        # that ends up in the leg's FROM/JOIN graph so filter applicability is
        # judged against what's actually available — this includes objects
        # joined solely to satisfy a WHERE filter (e.g. a dim object referenced
        # by a filter but not by any projected field).
        leg_objects: set[str] = {root} | {o for o in leg_required if o in model.data_objects}
        if len(leg_required) > 1:
            steps = graph.find_join_path({root}, leg_required)
            for step in steps:
                target = model.data_objects.get(step.to_object)
                if target is None:
                    continue
                on_expr = graph.build_join_condition(step)
                builder.join(
                    table=qualify(target),
                    on=on_expr,
                    join_type=step.join_type,
                    alias=step.to_object,
                )
                leg_objects.add(step.from_object)
                leg_objects.add(step.to_object)

        # WHERE — apply filters whose referenced objects are in this leg.
        # Filters that touch objects unreachable from this leg are skipped
        # because the columns they reference don't exist here.
        for wf in resolved.where_filters:
            ref_objects: set[str] = set()
            self._collect_table_refs(wf.expression, ref_objects)
            if ref_objects.issubset(leg_objects):
                builder.where(wf.expression)

        return builder.build()

    @staticmethod
    def _null_cast_for_field(field: ResolvedField, model: SemanticModel) -> Expr:
        """Return a typed ``CAST(NULL AS <type>)`` for the column's abstract type.

        Falls back to a bare NULL when the column or its type cannot be
        resolved — keeps codegen working without a hard error.
        """
        obj = model.data_objects.get(field.object_name)
        if obj is None:
            return Literal.null()
        column = obj.columns.get(field.column_name)
        if column is None:
            return Literal.null()
        type_name = column.abstract_type.value
        return Cast(Literal.null(), type_name=type_name)

    @staticmethod
    def _remap_order_by(expr: Expr, resolved: ResolvedQuery, model: SemanticModel) -> Expr:
        """Convert column references to the field alias for the CTE outer query.

        The CFL outer query selects from the composite CTE — original
        ``"DataObject"."COLUMN"`` references (and inlined computed-column
        expressions) are out of scope. Match each field by structural equality
        with its column expression so plain *and* computed columns map back
        to the field alias.
        """
        for f in resolved.fields:
            if expr == make_column_expr(model, f.object_name, f.column_name):
                return ColumnRef(name=f.alias)
        return expr

    @staticmethod
    def _collect_table_refs(expr: Expr, tables: set[str]) -> None:
        """Walk an expression tree collecting referenced table names.

        Mirrors ``CFLPlanner._collect_table_refs`` for the subset of node
        types raw-mode WHERE filters can produce. Imported lazily to avoid
        a circular import with ``compiler/cfl.py``.
        """
        from orionbelt.ast.nodes import (
            Between,
            BinaryOp,
            FunctionCall,
            InList,
            IsNull,
            RelativeDateRange,
            UnaryOp,
        )

        if isinstance(expr, ColumnRef) and expr.table:
            tables.add(expr.table)
        elif isinstance(expr, BinaryOp):
            RawPlanner._collect_table_refs(expr.left, tables)
            RawPlanner._collect_table_refs(expr.right, tables)
        elif isinstance(expr, UnaryOp):
            RawPlanner._collect_table_refs(expr.operand, tables)
        elif isinstance(expr, (InList, IsNull, Between)):
            RawPlanner._collect_table_refs(expr.expr, tables)
        elif isinstance(expr, RelativeDateRange):
            RawPlanner._collect_table_refs(expr.column, tables)
        elif isinstance(expr, FunctionCall):
            for arg in expr.args:
                RawPlanner._collect_table_refs(arg, tables)


__all__ = ["RawPlanner"]
