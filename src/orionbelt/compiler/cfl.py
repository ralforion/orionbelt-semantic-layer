"""CFL (Composite Fact Layer) planner: conformed dimensions + fact stitching."""

from __future__ import annotations

from collections.abc import Callable

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Except,
    Expr,
    FunctionCall,
    InList,
    IsNull,
    Join,
    JoinType,
    Literal,
    RelativeDateRange,
    Select,
    UnaryOp,
    UnionAll,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.resolution import ResolvedDimension, ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.star import CflLegInfo, QueryPlan
from orionbelt.compiler.type_resolver import resolve_measure_data_type, resolve_metric_data_type
from orionbelt.dialect.base import Dialect
from orionbelt.models.semantic import DataObject, SemanticModel

__all__ = ["CFLPlanner", "FanoutError"]


def _expand_cfl_measure_refs(expr: Expr, measure_exprs: dict[str, Expr]) -> Expr:
    """Replace bare ColumnRef aliases in HAVING with their full aggregate expressions."""
    if isinstance(expr, ColumnRef) and expr.table is None and expr.name in measure_exprs:
        return measure_exprs[expr.name]
    if isinstance(expr, BinaryOp):
        new_left = _expand_cfl_measure_refs(expr.left, measure_exprs)
        new_right = _expand_cfl_measure_refs(expr.right, measure_exprs)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    return expr


class CFLPlanner:
    """Plans Composite Fact Layer queries: conformed dimensions + fact stitching.

    Uses a UNION ALL strategy:
    1. Each fact leg SELECTs conformed dimensions + its own measures (NULL for others)
    2. UNION ALL combines the legs into a single CTE
    3. Outer query aggregates over the union, grouping by conformed dimensions
    """

    def plan(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
        union_by_name: bool = False,
        dialect: Dialect | None = None,
    ) -> QueryPlan:
        """Plan a CFL query."""
        self._validate_fanout(resolved, model)

        # dimensionsExclude: EXCEPT-based anti-join pattern
        if resolved.dimensions_exclude:
            return self._plan_dimensions_exclude(resolved, model, qualify_table)

        # Group measures by their source object
        measures_by_object, cross_fact = self._group_measures_by_object(resolved, model)

        # Dimension-only CFL: no measures but dimensions on independent branches.
        # Create leg groupings from connecting fact tables.
        if not measures_by_object and not cross_fact and resolved.requires_cfl:
            measures_by_object = self._group_dimensions_into_legs(resolved, model)

        if len(measures_by_object) <= 1 and not cross_fact:
            # Single fact — delegate to star schema
            from orionbelt.compiler.star import StarSchemaPlanner

            return StarSchemaPlanner().plan(
                resolved, model, qualify_table=qualify_table, dialect=dialect
            )

        # Multi-fact: UNION ALL strategy
        return self._plan_union_all(
            resolved,
            model,
            measures_by_object,
            cross_fact,
            qualify_table=qualify_table,
            union_by_name=union_by_name,
            dialect=dialect,
        )

    def _validate_fanout(self, resolved: ResolvedQuery, model: SemanticModel) -> None:
        """Validate that grain is compatible and no fanout will occur."""
        errors: list[str] = []

        for dim in resolved.dimensions:
            if dim.object_name not in model.data_objects:
                errors.append(
                    f"Dimension '{dim.name}' references unknown data object '{dim.object_name}'"
                )

        if errors:
            raise FanoutError("; ".join(errors))

    def _group_measures_by_object(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
    ) -> tuple[dict[str, list[ResolvedMeasure]], list[ResolvedMeasure]]:
        """Group measures by their primary source object.

        Returns ``(groups, cross_fact)`` where *cross_fact* contains
        multi-field measures whose fields span multiple objects.
        For metrics, expand their component measures into the grouping
        instead of the metric itself.  Cross-fact measures ensure every
        involved object has a leg, but are not assigned to any single
        group — their individual fields are distributed per-leg by
        ``_plan_union_all``.
        """
        groups: dict[str, list[ResolvedMeasure]] = {}
        cross_fact: list[ResolvedMeasure] = []
        seen: set[str] = set()

        for measure in resolved.measures:
            if measure.component_measures:
                # Metric: add each component measure to its source object
                for comp_name in measure.component_measures:
                    if comp_name in seen:
                        continue
                    seen.add(comp_name)
                    comp = resolved.metric_components.get(comp_name)
                    if comp is None:
                        continue
                    model_measure = model.measures.get(comp_name)
                    if model_measure and model_measure.columns:
                        obj_name = model_measure.columns[0].view or resolved.base_object
                    else:
                        obj_name = resolved.base_object
                    groups.setdefault(obj_name, []).append(comp)
            else:
                if measure.name in seen:
                    continue
                seen.add(measure.name)
                model_measure = model.measures.get(measure.name)
                if not model_measure:
                    groups.setdefault(resolved.base_object, []).append(measure)
                    continue

                # Collect source objects: from explicit columns or expression AST
                field_objects: set[str]
                if model_measure.columns:
                    field_objects = {f.view for f in model_measure.columns if f.view}
                else:
                    # Expression-based measure: extract table refs from the AST
                    field_objects = set()
                    self._collect_table_refs(measure.expression, field_objects)
                if len(field_objects) > 1:
                    # Cross-fact multi-field measure: ensure each
                    # involved object has a leg, but don't assign
                    # the measure to any single group.
                    cross_fact.append(measure)
                    for obj in field_objects:
                        groups.setdefault(obj, [])
                elif field_objects:
                    obj_name = next(iter(field_objects))
                    groups.setdefault(obj_name, []).append(measure)
                else:
                    groups.setdefault(resolved.base_object, []).append(measure)

        return groups, cross_fact

    @staticmethod
    def _group_dimensions_into_legs(
        resolved: ResolvedQuery,
        model: SemanticModel,
    ) -> dict[str, list[ResolvedMeasure]]:
        """Group dimensions into CFL legs for dimension-only queries.

        For each dimension, find the fact/bridge table that can reach it
        via directed join paths, and use that as the leg's key object.
        Returns empty measure lists per leg (dimension-only, no aggregates).
        """
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)
        legs: dict[str, list[ResolvedMeasure]] = {}
        assigned: set[str] = set()

        # Build a lookup: for each dimension object, which fact tables can reach it?
        dim_objects = {d.object_name for d in resolved.dimensions}
        fact_candidates: list[tuple[str, set[str]]] = []
        for obj_name, obj in model.data_objects.items():
            if not obj.joins:
                continue
            reachable_dims = dim_objects & (graph.descendants(obj_name) | {obj_name})
            if reachable_dims:
                fact_candidates.append((obj_name, reachable_dims))

        # Greedy: pick fact table covering most unassigned dimensions first
        fact_candidates.sort(key=lambda x: (-len(x[1]), x[0]))
        for fact_obj, reachable in fact_candidates:
            covers = reachable - assigned
            if covers:
                legs[fact_obj] = []
                assigned.update(covers)

        return legs

    @staticmethod
    def _is_multi_field(measure: ResolvedMeasure) -> bool:
        """Check if a measure has multiple field args (e.g. COUNT(a, b))."""
        return isinstance(measure.expression, FunctionCall) and len(measure.expression.args) > 1

    @staticmethod
    def _resolve_null_type_for_field(
        measure: ResolvedMeasure,
        field_idx: int,
        model: SemanticModel,
    ) -> str | None:
        """Look up the abstract type for a multi-field measure's *field_idx*-th column.

        Falls back to the measure's ``result_type`` if the column cannot be found.
        """
        model_measure = model.measures.get(measure.name)
        if not model_measure:
            return None
        # Try to find the column's abstract_type from the data object
        if field_idx < len(model_measure.columns):
            ref = model_measure.columns[field_idx]
            obj = model.data_objects.get(ref.view) if ref.view else None
            if obj and ref.column in obj.columns:
                return obj.columns[ref.column].abstract_type.value
        # Fallback to measure result_type
        return model_measure.result_type.value

    @staticmethod
    def _multi_field_cte_alias(measure_name: str, idx: int) -> str:
        """CTE column name for the *idx*-th field of a multi-field measure."""
        return f"{measure_name}__f{idx}"

    @staticmethod
    def _unwrap_aggregation(measure: ResolvedMeasure) -> Expr:
        """Extract the inner expression from an aggregated measure.

        For FunctionCall(SUM, [inner]) → returns inner.
        Falls back to the full expression if not a FunctionCall.
        """
        if isinstance(measure.expression, FunctionCall) and measure.expression.args:
            return measure.expression.args[0]
        return measure.expression

    def _build_outer_metric_expr(
        self,
        metric: ResolvedMeasure,
        resolved: ResolvedQuery,
    ) -> Expr:
        """Build the outer query expression for a metric.

        Walks the metric's AST tree and replaces each ColumnRef(measure_name)
        with ``AGG("measure_name")`` using the component measure's aggregation.
        """
        return self._substitute_outer_refs(metric.expression, resolved)

    def _substitute_outer_refs(self, expr: Expr, resolved: ResolvedQuery) -> Expr:
        """Recursively substitute measure refs with outer aggregations."""
        if isinstance(expr, ColumnRef) and expr.table is None:
            comp = resolved.metric_components.get(expr.name)
            if comp:
                agg = comp.aggregation.upper()
                distinct = False
                if agg == "COUNT_DISTINCT":
                    agg = "COUNT"
                    distinct = True
                if isinstance(comp.expression, FunctionCall) and comp.expression.distinct:
                    distinct = True
                return FunctionCall(
                    name=agg,
                    args=[ColumnRef(name=comp.name)],
                    distinct=distinct,
                )
        if isinstance(expr, BinaryOp):
            new_left = self._substitute_outer_refs(expr.left, resolved)
            new_right = self._substitute_outer_refs(expr.right, resolved)
            if new_left is not expr.left or new_right is not expr.right:
                return BinaryOp(left=new_left, op=expr.op, right=new_right)
        return expr

    @staticmethod
    def _collect_table_refs(expr: Expr, tables: set[str]) -> None:
        """Recursively collect table names from ColumnRef nodes."""
        if isinstance(expr, ColumnRef) and expr.table:
            tables.add(expr.table)
        elif isinstance(expr, BinaryOp):
            CFLPlanner._collect_table_refs(expr.left, tables)
            CFLPlanner._collect_table_refs(expr.right, tables)
        elif isinstance(expr, UnaryOp):
            CFLPlanner._collect_table_refs(expr.operand, tables)
        elif isinstance(expr, (InList, IsNull, Between)):
            CFLPlanner._collect_table_refs(expr.expr, tables)
        elif isinstance(expr, RelativeDateRange):
            CFLPlanner._collect_table_refs(expr.column, tables)
        elif isinstance(expr, FunctionCall):
            for arg in expr.args:
                CFLPlanner._collect_table_refs(arg, tables)

    @staticmethod
    def _remap_cfl_order_by(expr: Expr, resolved: ResolvedQuery) -> Expr:
        """Remap ORDER BY expressions to use CTE aliases for the outer query.

        In CFL, the outer query selects from the composite CTE — original
        table-qualified refs are out of scope.  Remap dimension and measure
        expressions to their CTE alias names.
        """
        # Dimension: ColumnRef(name=source_col, table=obj) → ColumnRef(name=dim.name)
        if isinstance(expr, ColumnRef) and expr.table is not None:
            for dim in resolved.dimensions:
                if expr.name == dim.source_column and expr.table == dim.object_name:
                    return ColumnRef(name=dim.name)
        # Measure: match by identity (same expression object)
        for meas in resolved.measures:
            if expr is meas.expression:
                return ColumnRef(name=meas.name)
        # Numeric position — pass through
        return expr

    def _build_outer_concat_count(
        self,
        measure_name: str,
        n_fields: int,
        agg: str,
        distinct: bool,
    ) -> Expr:
        """Build ``COUNT(DISTINCT CAST(f0 AS VARCHAR) || '|' || ...)`` for the outer query."""
        parts: list[Expr] = [
            Cast(
                expr=ColumnRef(name=self._multi_field_cte_alias(measure_name, i)),
                type_name="VARCHAR",
            )
            for i in range(n_fields)
        ]
        concat: Expr = parts[0]
        for part in parts[1:]:
            concat = BinaryOp(
                left=concat,
                op="||",
                right=BinaryOp(
                    left=Literal.string("|"),
                    op="||",
                    right=part,
                ),
            )
        return FunctionCall(name=agg, args=[concat], distinct=distinct)

    def _plan_union_all(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        measures_by_object: dict[str, list[ResolvedMeasure]],
        cross_fact: list[ResolvedMeasure] | None = None,
        qualify_table: Callable[[DataObject], str] | None = None,
        union_by_name: bool = False,
        dialect: Dialect | None = None,
    ) -> QueryPlan:
        """UNION ALL strategy: stack fact legs with NULL padding, aggregate outside.

        When *union_by_name* is True (DuckDB, Snowflake) each leg only emits
        the columns it actually has — the database fills missing columns with
        NULL automatically via ``UNION ALL BY NAME``.
        """
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        # Collect all measures across all objects + cross-fact measures
        all_measures: list[ResolvedMeasure] = []
        for measures in measures_by_object.values():
            all_measures.extend(measures)
        if cross_fact:
            all_measures.extend(cross_fact)

        # Collect data objects referenced by WHERE filters — each leg
        # must join these tables so the filter predicates are valid.
        filter_objects: set[str] = set()
        for wf in resolved.where_filters:
            self._collect_table_refs(wf.expression, filter_objects)

        # Build one SELECT per fact object group.
        # Each leg computes its own LCA (least common ancestor) as the lead
        # table — the graph-central node that can reach all dimension objects
        # and the measure's source object with minimal hops.
        union_legs: list[Select] = []
        leg_infos: list[CflLegInfo] = []
        for obj_name, measures in measures_by_object.items():
            leg_builder = QueryBuilder()
            this_measure_names = {m.name for m in measures}

            # Compute reachability from this leg's fact object upfront
            reachable = graph.descendants(obj_name) | {obj_name}

            # SELECT conformed dimensions — only emit real column refs for
            # dimensions reachable from this leg's fact; skip unreachable
            # ones when the dialect supports UNION ALL BY NAME.
            for dim in resolved.dimensions:
                if dim.object_name in reachable:
                    col: Expr = ColumnRef(name=dim.source_column, table=dim.object_name)
                    if dim.grain and dialect:
                        col = dialect.render_time_grain(col, dim.grain)
                    leg_builder.select(AliasedExpr(expr=col, alias=dim.name))
                elif not union_by_name:
                    model_dim = model.dimensions.get(dim.name)
                    dim_type = model_dim.result_type.value if model_dim else None
                    col = Cast(Literal.null(), type_name=dim_type) if dim_type else Literal.null()
                    leg_builder.select(AliasedExpr(expr=col, alias=dim.name))

            # SELECT this fact's measures (raw expressions, no aggregation).
            # When union_by_name is True, skip NULL padding for other facts'
            # measures — the database fills them automatically.
            for m in all_measures:
                if self._is_multi_field(m):
                    assert isinstance(m.expression, FunctionCall)
                    for i, arg in enumerate(m.expression.args):
                        alias = self._multi_field_cte_alias(m.name, i)
                        arg_table = arg.table if isinstance(arg, ColumnRef) else None
                        if arg_table == obj_name:
                            leg_builder.select(AliasedExpr(expr=arg, alias=alias))
                        elif not union_by_name:
                            null_type = self._resolve_null_type_for_field(m, i, model)
                            null_expr: Expr = (
                                Cast(Literal.null(), type_name=null_type)
                                if null_type
                                else Literal.null()
                            )
                            leg_builder.select(AliasedExpr(expr=null_expr, alias=alias))
                elif m.name in this_measure_names:
                    leg_builder.select(AliasedExpr(expr=self._unwrap_aggregation(m), alias=m.name))
                elif not union_by_name:
                    model_measure = model.measures.get(m.name)
                    null_type_name = self._resolve_null_type_for_field(m, 0, model)
                    if null_type_name is None and model_measure:
                        null_type_name = model_measure.result_type.value
                    null_expr = (
                        Cast(Literal.null(), type_name=null_type_name)
                        if null_type_name
                        else Literal.null()
                    )
                    leg_builder.select(AliasedExpr(expr=null_expr, alias=m.name))

            # Determine the common root for this leg:
            # the deepest directed ancestor that can reach all dimension
            # objects, measure's source object, and filter-referenced objects.
            # Only include dimensions reachable from this leg's fact object.
            leg_required = {
                dim.object_name for dim in resolved.dimensions if dim.object_name in reachable
            }
            leg_required.add(obj_name)
            leg_required.update(filter_objects)
            lead = graph.find_common_root(leg_required)
            lead_obj = model.data_objects.get(lead)

            # FROM: the lead (LCA) table
            if lead_obj:
                leg_builder.from_(qualify(lead_obj), alias=lead)

            # JOINs: all required objects reachable from the lead
            join_targets = leg_required - {lead}
            steps: list[JoinStep] = []
            if join_targets:
                steps = graph.find_join_path({lead}, leg_required)
                for step in steps:
                    target_object = model.data_objects.get(step.to_object)
                    if target_object:
                        on_expr = graph.build_join_condition(step)
                        leg_builder.join(
                            table=qualify(target_object),
                            on=on_expr,
                            join_type=step.join_type,
                            alias=step.to_object,
                        )

            # Capture leg info for explain
            leg_join_strs = (
                [f"{s.from_object} → {s.to_object}" for s in steps] if join_targets else []
            )
            if lead == obj_name:
                leg_reason = (
                    f'"{lead}" is the measure source — '
                    f"all required dimension objects are reachable from it"
                )
            else:
                leg_reason = (
                    f'"{lead}" is the deepest common root that can reach '
                    f'measure source "{obj_name}" and all reachable dimension objects'
                )
            leg_infos.append(
                CflLegInfo(
                    measure_source=obj_name,
                    common_root=lead,
                    reason=leg_reason,
                    measures=[m.name for m in measures],
                    joins=leg_join_strs,
                )
            )

            # Apply WHERE filters to each leg
            for wf in resolved.where_filters:
                leg_builder.where(wf.expression)

            union_legs.append(leg_builder.build())

        # Create the UNION ALL CTE
        cte_name = "composite_01"
        union_cte = CTE(name=cte_name, query=UnionAll(queries=union_legs))

        # Build outer query: aggregate over the composite CTE
        outer_builder = QueryBuilder()

        # SELECT dimensions
        for dim in resolved.dimensions:
            outer_builder.select(
                AliasedExpr(
                    expr=ColumnRef(name=dim.name),
                    alias=dim.name,
                )
            )

        # SELECT aggregated measures and metrics
        # First, add all component measures (from UNION ALL legs)
        settings = model.settings
        seen_measure_names: set[str] = set()
        outer_measure_exprs: dict[str, Expr] = {}
        for m in all_measures:
            seen_measure_names.add(m.name)
            agg = m.aggregation.upper()
            distinct = False
            if agg == "COUNT_DISTINCT":
                agg = "COUNT"
                distinct = True
            if isinstance(m.expression, FunctionCall) and m.expression.distinct:
                distinct = True

            if self._is_multi_field(m):
                # Multi-field: concat CTE columns in outer query
                assert isinstance(m.expression, FunctionCall)
                n_fields = len(m.expression.args)
                agg_expr: Expr = self._build_outer_concat_count(m.name, n_fields, agg, distinct)
            else:
                agg_expr = FunctionCall(
                    name=agg,
                    args=[ColumnRef(name=m.name)],
                    distinct=distinct,
                )
            # Apply CAST for resolved data_type
            model_measure = model.measures.get(m.name)
            if model_measure and dialect:
                resolved_type = resolve_measure_data_type(model_measure, settings)
                if resolved_type:
                    type_sql = dialect.render_obml_type(resolved_type)
                    agg_expr = Cast(expr=agg_expr, type_name=type_sql)
            outer_builder.select(AliasedExpr(expr=agg_expr, alias=m.name))
            outer_measure_exprs[m.name] = agg_expr

        # Then, add metric expressions that combine component measures
        for m in resolved.measures:
            if m.component_measures and m.name not in seen_measure_names:
                metric_expr: Expr = self._build_outer_metric_expr(m, resolved)
                metric = model.metrics.get(m.name)
                if metric and dialect:
                    resolved_type = resolve_metric_data_type(metric, settings)
                    if resolved_type:
                        type_sql = dialect.render_obml_type(resolved_type)
                        metric_expr = Cast(expr=metric_expr, type_name=type_sql)
                outer_builder.select(AliasedExpr(expr=metric_expr, alias=m.name))
                outer_measure_exprs[m.name] = metric_expr

        outer_builder.from_(cte_name, alias=cte_name)

        # GROUP BY dimensions
        for dim in resolved.dimensions:
            outer_builder.group_by(ColumnRef(name=dim.name))

        # HAVING — expand alias references to actual CAST'd aggregate expressions
        for hf in resolved.having_filters:
            outer_builder.having(_expand_cfl_measure_refs(hf.expression, outer_measure_exprs))

        # ORDER BY and LIMIT — remap to CTE aliases
        for expr, desc in resolved.order_by_exprs:
            outer_builder.order_by(self._remap_cfl_order_by(expr, resolved), desc=desc)
        if resolved.limit is not None:
            outer_builder.limit(resolved.limit)
        if resolved.offset is not None:
            outer_builder.offset(resolved.offset)

        outer_select = outer_builder.build()

        # Attach CTE
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
        )

        return QueryPlan(ast=final, cfl_legs=leg_infos)

    # -- dimensionsExclude: EXCEPT-based anti-join ----------------------------

    def _plan_dimensions_exclude(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
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
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        # Partition dimensions into independent groups
        dim_groups = self._partition_dimensions(resolved, graph)

        ctes: list[CTE] = []

        # CTE per dimension group: SELECT DISTINCT via GROUP BY
        group_cte_names: list[str] = []
        for i, group_dims in enumerate(dim_groups):
            cte_name = f"dim_group_{i:02d}"
            group_cte_names.append(cte_name)
            cte_query = self._build_group_distinct_select(group_dims, model, graph, qualify)
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

        # Build "existing_pairs": actual combinations via fact-table joins
        existing_pairs_select = self._build_existing_pairs_select(resolved, model, graph, qualify)

        # EXCEPT CTE: all_pairs EXCEPT existing_pairs
        except_cte = CTE(
            name="non_combinations",
            query=Except(left=all_pairs_select, right=existing_pairs_select),
        )
        ctes.append(except_cte)

        # Outer query: SELECT from non_combinations with ORDER BY / LIMIT
        outer_builder = QueryBuilder()
        for dim in resolved.dimensions:
            outer_builder.select(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
        outer_builder.from_("non_combinations", alias="non_combinations")

        for expr, desc in resolved.order_by_exprs:
            outer_builder.order_by(self._remap_cfl_order_by(expr, resolved), desc=desc)
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

    @staticmethod
    def _partition_dimensions(
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
            reachable = graph.descendants(obj) | {obj}
            for other in objects:
                if (
                    other != obj
                    and other not in assigned
                    and (other in reachable or obj in (graph.descendants(other) | {other}))
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

    @staticmethod
    def _build_group_distinct_select(
        dims: list[ResolvedDimension],
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
    ) -> Select:
        """Build SELECT DISTINCT (via GROUP BY) for a group of dimensions."""
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
            col: Expr = ColumnRef(name=dim.source_column, table=dim.object_name)
            builder.select(AliasedExpr(expr=col, alias=dim.name))
            builder.group_by(col)

        if root_obj:
            builder.from_(qualify(root_obj), alias=root)

        # Join to reach all dimension objects from root
        all_needed = required_objects | {root}
        if len(all_needed) > 1:
            steps = graph.find_join_path({root}, all_needed)
            for step in steps:
                target_obj = model.data_objects.get(step.to_object)
                if target_obj:
                    on_expr = graph.build_join_condition(step)
                    builder.join(
                        table=qualify(target_obj),
                        on=on_expr,
                        join_type=step.join_type,
                        alias=step.to_object,
                    )

        return builder.build()

    def _build_existing_pairs_select(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
    ) -> Select:
        """Build SELECT for existing dimension combinations via fact-table joins.

        Uses a fact/bridge table as the base and joins through hub tables
        to reach all dimension objects on both branches.
        """
        all_dim_objects = {d.object_name for d in resolved.dimensions}

        # Find fact tables that connect the dimension groups
        leg_objects = self._group_dimensions_into_legs(resolved, model)
        fact_tables = set(leg_objects.keys())

        # Use a fact table as the base (pick the one with most joins)
        best_fact = max(
            sorted(fact_tables),
            key=lambda f: len(model.data_objects[f].joins) if f in model.data_objects else 0,
        )
        best_fact_obj = model.data_objects.get(best_fact)

        builder = QueryBuilder()
        for dim in resolved.dimensions:
            col: Expr = ColumnRef(name=dim.source_column, table=dim.object_name)
            builder.select(AliasedExpr(expr=col, alias=dim.name))
            builder.group_by(col)

        if best_fact_obj:
            builder.from_(qualify(best_fact_obj), alias=best_fact)

        # Required: all dimension objects + all fact tables
        all_needed = all_dim_objects | fact_tables | {best_fact}
        joined: set[str] = {best_fact}
        steps = graph.find_join_path({best_fact}, all_needed)
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

        # Apply WHERE filters to existing pairs
        for wf in resolved.where_filters:
            builder.where(wf.expression)

        return builder.build()
