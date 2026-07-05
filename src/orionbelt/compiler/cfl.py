"""CFL (Composite Fact Layer) planner: conformed dimensions + fact stitching."""

from __future__ import annotations

from collections.abc import Callable

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    Select,
    UnionAll,
)
from orionbelt.compiler import cfl_exclude, cfl_projection
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.resolution import (
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
)
from orionbelt.compiler.star import CflLegInfo, QueryPlan, _grouping_flag_alias, _nulls_last
from orionbelt.compiler.type_resolver import resolve_measure_data_type, resolve_metric_data_type
from orionbelt.dialect.base import Dialect, UnsupportedAggregationError
from orionbelt.models.semantic import (
    TWO_COLUMN_AGGREGATIONS,
    DataObject,
    SemanticModel,
)


class UnsupportedAggregationForCFLError(UnsupportedAggregationError):
    """Raised when a measure's aggregation cannot be planned in CFL.

    Two-column statistical aggregates (``corr``, ``covar_*``, ``regr_*``)
    need paired-row semantics that the current UNION ALL + concat-count
    CFL strategy cannot express. The single-fact path (star planner)
    handles them correctly; only multi-fact CFL trips this guard.

    Inherits ``UnsupportedAggregationError`` so existing router catch
    sites surface the same 422 response shape. The ``dialect`` slot
    carries the planner identifier (``"cfl"``) rather than a SQL
    dialect — kept for response compatibility.
    """

    def __init__(self, measure_name: str, aggregation: str) -> None:
        self.measure_name = measure_name
        # Skip parent ``__init__`` (which formats a dialect-flavored
        # message) and set the same fields directly with our CFL-specific
        # message so routers and tests still get the structured
        # ``.dialect`` / ``.aggregation`` attributes.
        self.dialect = "cfl"
        self.aggregation = aggregation
        Exception.__init__(
            self,
            f"Measure '{measure_name}' uses a two-column statistical aggregate "
            f"({aggregation.upper()}) that needs paired rows from one fact table, "
            "but this query combines measures from more than one fact, so the rows "
            f"can't be paired. Query '{measure_name}' on its own, or only alongside "
            "measures and dimensions from its own fact table.",
        )


__all__ = ["CFLPlanner", "FanoutError", "UnsupportedAggregationForCFLError"]


def _expand_cfl_measure_refs(expr: Expr, measure_exprs: dict[str, Expr]) -> Expr:
    """Replace bare ColumnRef aliases in HAVING with their full aggregate expressions.

    Thin delegator to :func:`cfl_projection.expand_cfl_measure_refs`.
    """
    return cfl_projection.expand_cfl_measure_refs(expr, measure_exprs)


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

        # Two-column statistical aggregates (CORR/COVAR_*/REGR_*) need
        # paired-row semantics that the UNION ALL + concat-count multi-fact
        # path cannot express. Without this guard the planner emits
        # ``CORR(CAST(f0 AS VARCHAR) || '|' || CAST(f1 AS VARCHAR))`` — one
        # argument, wrong type. Fail fast with a clear error so the caller
        # can restructure their model or restrict the query to a single
        # fact source instead of getting an opaque execution-time error.
        for measure in resolved.measures:
            agg = measure.aggregation.lower() if measure.aggregation else ""
            if agg in TWO_COLUMN_AGGREGATIONS:
                raise UnsupportedAggregationForCFLError(measure.name, agg)

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
        """Group measures by their primary source object."""
        return cfl_projection.group_measures_by_object(self, resolved, model)

    @staticmethod
    def _group_dimensions_into_legs(
        resolved: ResolvedQuery,
        model: SemanticModel,
    ) -> dict[str, list[ResolvedMeasure]]:
        """Group dimensions into CFL legs for dimension-only queries."""
        return cfl_projection.group_dimensions_into_legs(resolved, model)

    @staticmethod
    def _is_multi_field(measure: ResolvedMeasure) -> bool:
        """Check if a measure has multiple field args (e.g. COUNT(a, b))."""
        return cfl_projection.is_multi_field(measure)

    @staticmethod
    def _resolve_null_type_for_field(
        measure: ResolvedMeasure,
        field_idx: int,
        model: SemanticModel,
        dialect: Dialect | None = None,
    ) -> str | None:
        """Resolve the SQL type for NULL padding in CFL UNION ALL legs."""
        return cfl_projection.resolve_null_type_for_field(measure, field_idx, model, dialect)

    @staticmethod
    def _multi_field_cte_alias(measure_name: str, idx: int) -> str:
        """CTE column name for the *idx*-th field of a multi-field measure."""
        return cfl_projection.multi_field_cte_alias(measure_name, idx)

    @staticmethod
    def _unwrap_aggregation(measure: ResolvedMeasure) -> Expr:
        """Extract the inner expression from an aggregated measure."""
        return cfl_projection.unwrap_aggregation(measure)

    def _build_outer_metric_expr(
        self,
        metric: ResolvedMeasure,
        resolved: ResolvedQuery,
        cte_name: str,
    ) -> Expr:
        """Build the outer query expression for a metric."""
        return cfl_projection.build_outer_metric_expr(self, metric, resolved, cte_name)

    def _substitute_outer_refs(self, expr: Expr, resolved: ResolvedQuery, cte_name: str) -> Expr:
        """Recursively substitute measure refs with outer aggregations."""
        return cfl_projection.substitute_outer_refs(self, expr, resolved, cte_name)

    @staticmethod
    def _collect_table_refs(expr: Expr, tables: set[str]) -> None:
        """Recursively collect table names from ColumnRef nodes."""
        cfl_projection.collect_table_refs(expr, tables)

    @staticmethod
    def _remap_cfl_order_by(expr: Expr, resolved: ResolvedQuery, model: SemanticModel) -> Expr:
        """Remap ORDER BY expressions to use CTE aliases for the outer query."""
        return cfl_projection.remap_cfl_order_by(expr, resolved, model)

    def _build_outer_concat_count(
        self,
        measure_name: str,
        n_fields: int,
        agg: str,
        distinct: bool,
        cte_name: str,
    ) -> Expr:
        """Build ``COUNT(DISTINCT CAST(f0 AS VARCHAR) || '|' || ...)`` for the outer query."""
        return cfl_projection.build_outer_concat_count(
            self, measure_name, n_fields, agg, distinct, cte_name
        )

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

            # Collect table references from this leg's own-measure
            # expressions. A measure like ``Electronics Sales`` is
            # defined as ``SUM(CASE WHEN Products.productcat = …
            # THEN Sales.salesamount END)`` — the CASE condition
            # references Products, which must be joined into this
            # leg's FROM. Without this, the generated SQL emits
            # ``"Products"."productcat"`` against a FROM clause that
            # only has Sales + Clients, and the database raises
            # "missing FROM-clause entry for table Products".
            measure_expr_objects: set[str] = set()
            for m in measures:
                self._collect_table_refs(m.expression, measure_expr_objects)
            if cross_fact:
                for m in cross_fact:
                    if m.name in this_measure_names:
                        self._collect_table_refs(m.expression, measure_expr_objects)

            # SELECT conformed dimensions — only emit real column refs for
            # dimensions reachable from this leg's fact AND whose `via:`
            # waypoint (if any) is also reachable from this leg's fact.
            # Role-playing dimensions tied to a different fact via `via:`
            # are NULL-padded so each leg only projects the values that
            # belong to its own fact.
            for dim in resolved.dimensions:
                via_ok = dim.via is None or dim.via in reachable
                if dim.object_name in reachable and via_ok:
                    col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
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
                    # Cast the own-measure column to the same type used for
                    # NULL padding in sibling legs, so every leg's column
                    # agrees on a single type. Without this, strict-typed
                    # engines (ClickHouse with UNION ALL) produce a Variant
                    # type that SUM can't aggregate ("ILLEGAL_TYPE_OF_ARGUMENT
                    # Variant(Decimal, Float64)").
                    own_expr: Expr = self._unwrap_aggregation(m)
                    own_type_name = self._resolve_null_type_for_field(m, 0, model, dialect)
                    if own_type_name:
                        own_expr = Cast(expr=own_expr, type_name=own_type_name)
                    leg_builder.select(AliasedExpr(expr=own_expr, alias=m.name))
                elif not union_by_name:
                    model_measure = model.measures.get(m.name)
                    null_type_name = self._resolve_null_type_for_field(m, 0, model, dialect)
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
            # objects, measure's source object, filter-referenced objects,
            # and any objects referenced by this leg's measure expressions.
            # Only include dimensions reachable from this leg's fact object.
            leg_required = {
                dim.object_name for dim in resolved.dimensions if dim.object_name in reachable
            }
            leg_required.add(obj_name)
            leg_required.update(filter_objects)
            # Include objects referenced by measure expressions, but only
            # those reachable from this leg's fact — cross-fact filter
            # tables would otherwise pull unrelated facts into the leg.
            leg_required.update(measure_expr_objects & reachable)
            lead = graph.find_common_root(leg_required)
            lead_obj = model.data_objects.get(lead)

            # FROM: the lead (LCA) table
            if lead_obj:
                leg_builder.from_(qualify(lead_obj), alias=lead)

            # JOINs: all required objects reachable from the lead
            join_targets = leg_required - {lead}
            steps: list[JoinStep] = []
            if join_targets:
                steps = graph.find_join_path(
                    {lead},
                    leg_required,
                    via_constraints=resolved.via_constraints or None,
                )
                # Dedupe by alias so a dim reachable through multiple
                # paths within one leg emits only one JOIN — postgres
                # rejects "table specified more than once" when two
                # role-played dims resolve to the same target object.
                joined_aliases: set[str] = {lead}
                for step in steps:
                    if step.to_object in joined_aliases:
                        continue
                    target_object = model.data_objects.get(step.to_object)
                    if target_object:
                        on_expr = graph.build_join_condition(step)
                        leg_builder.join(
                            table=qualify(target_object),
                            on=on_expr,
                            join_type=step.join_type,
                            alias=step.to_object,
                        )
                        joined_aliases.add(step.to_object)

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
        # All ColumnRefs that resolve to raw CTE columns inside outer-query
        # aggregate functions are qualified with *cte_name*. ClickHouse otherwise
        # resolves bare identifiers to sibling SELECT aliases first — when those
        # aliases are themselves aggregates (the case for measures and metrics
        # in the outer SELECT), it rejects the resulting nested aggregate as
        # ``ILLEGAL_AGGREGATION``. The qualification is harmless on dialects
        # that resolve column-first.

        # Build outer query: aggregate over the composite CTE
        outer_builder = QueryBuilder()

        # SELECT dimensions.  Coalesce groups emit COALESCE(d1, d2, ...) once
        # under the alias; plain dims keep their original column reference.
        emitted_coalesce_aliases: set[str] = set()
        coalesce_groups: dict[str, list[str]] = {}
        for d in resolved.dimensions:
            if d.coalesce_alias:
                coalesce_groups.setdefault(d.coalesce_alias, []).append(d.name)
        for dim in resolved.dimensions:
            if dim.coalesce_alias:
                if dim.coalesce_alias in emitted_coalesce_aliases:
                    continue
                emitted_coalesce_aliases.add(dim.coalesce_alias)
                outer_builder.select(
                    AliasedExpr(
                        expr=FunctionCall(
                            name="COALESCE",
                            args=[
                                ColumnRef(name=member)
                                for member in coalesce_groups[dim.coalesce_alias]
                            ],
                        ),
                        alias=dim.coalesce_alias,
                    )
                )
            else:
                outer_builder.select(
                    AliasedExpr(
                        expr=ColumnRef(name=dim.name),
                        alias=dim.name,
                    )
                )

        # SELECT aggregated measures and metrics
        # First, aggregate every measure from the UNION ALL legs. This
        # includes component measures pulled in only to feed a metric
        # (e.g. Total Returns / Total Purchases behind Return Rate /
        # Gross Margin). We still compute their aggregate expression and
        # record it in ``outer_measure_exprs`` so HAVING can reference any
        # measure, but we only PROJECT the measures the caller actually
        # requested — otherwise the result carries extra columns the
        # consumer never asked for, which Postgres-federation clients
        # (Dremio) reject as an unexpected dataset shape.
        settings = model.settings
        requested_measure_names = {rm.name for rm in resolved.measures}
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
                agg_expr: Expr = self._build_outer_concat_count(
                    m.name, n_fields, agg, distinct, cte_name
                )
            else:
                agg_expr = FunctionCall(
                    name=agg,
                    args=[ColumnRef(name=m.name, table=cte_name)],
                    distinct=distinct,
                )
            # Apply CAST for resolved data_type (effective_measures so
            # multi-fact synthesized counts get the same integer CAST as
            # declared count measures).
            model_measure = model.effective_measures.get(m.name)
            if model_measure and dialect:
                resolved_type = resolve_measure_data_type(model_measure, settings)
                if resolved_type:
                    agg_expr = dialect.cast_to_obml_type(agg_expr, resolved_type)
            if m.name in requested_measure_names:
                outer_builder.select(AliasedExpr(expr=agg_expr, alias=m.name))
            outer_measure_exprs[m.name] = agg_expr

        # Then, add metric expressions that combine component measures
        for m in resolved.measures:
            if m.component_measures and m.name not in seen_measure_names:
                metric_expr: Expr = self._build_outer_metric_expr(m, resolved, cte_name)
                metric = model.metrics.get(m.name)
                if metric and dialect:
                    resolved_type = resolve_metric_data_type(metric, settings)
                    if resolved_type:
                        metric_expr = dialect.cast_to_obml_type(metric_expr, resolved_type)
                outer_builder.select(AliasedExpr(expr=metric_expr, alias=m.name))
                outer_measure_exprs[m.name] = metric_expr

        outer_builder.from_(cte_name, alias=cte_name)

        # GROUP BY dimensions.  Coalesce groups group by the COALESCE expression
        # itself (most dialects accept either the alias or the expression; the
        # expression is portable across all eight supported dialects).
        grouped_coalesce_aliases: set[str] = set()
        for dim in resolved.dimensions:
            if dim.coalesce_alias:
                if dim.coalesce_alias in grouped_coalesce_aliases:
                    continue
                grouped_coalesce_aliases.add(dim.coalesce_alias)
                outer_builder.group_by(
                    FunctionCall(
                        name="COALESCE",
                        args=[
                            ColumnRef(name=member) for member in coalesce_groups[dim.coalesce_alias]
                        ],
                    )
                )
            else:
                outer_builder.group_by(ColumnRef(name=dim.name))

        # GROUPING() flag columns + grouping modifier (rollup/cube) — outer query only
        # so subtotal rows compose correctly over the unioned facts (the
        # individual UNION ALL legs stay at detail grain).
        if resolved.grouping is not None and resolved.dimensions:
            outer_builder.grouping(resolved.grouping.value)
            flag_aliases: list[str] = []
            for dim in resolved.dimensions:
                alias_name = dim.coalesce_alias or dim.name
                if alias_name in flag_aliases:
                    continue
                flag_aliases.append(alias_name)
            for alias in flag_aliases:
                flag_col = FunctionCall(name="GROUPING", args=[ColumnRef(name=alias)])
                outer_builder.select(AliasedExpr(expr=flag_col, alias=_grouping_flag_alias(alias)))

        # HAVING — expand alias references to actual CAST'd aggregate expressions
        for hf in resolved.having_filters:
            outer_builder.having(_expand_cfl_measure_refs(hf.expression, outer_measure_exprs))

        # ORDER BY and LIMIT — remap to CTE aliases
        for expr, desc, nulls in resolved.order_by_exprs:
            outer_builder.order_by(
                self._remap_cfl_order_by(expr, resolved, model),
                desc=desc,
                nulls_last=_nulls_last(nulls),
            )
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
            grouping=outer_select.grouping,
        )

        return QueryPlan(ast=final, cfl_legs=leg_infos)

    # -- dimensionsExclude: EXCEPT-based anti-join ----------------------------

    def _plan_dimensions_exclude(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
    ) -> QueryPlan:
        """Plan a dimensionsExclude query using EXCEPT pattern."""
        return cfl_exclude.plan_dimensions_exclude(self, resolved, model, qualify_table)

    @staticmethod
    def _partition_dimensions(
        resolved: ResolvedQuery,
        graph: JoinGraph,
    ) -> list[list[ResolvedDimension]]:
        """Partition dimensions into groups on independent branches."""
        return cfl_exclude.partition_dimensions(resolved, graph)

    @staticmethod
    def _build_group_distinct_select(
        dims: list[ResolvedDimension],
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
        via_constraints: dict[str, str] | None = None,
    ) -> Select:
        """Build SELECT DISTINCT (via GROUP BY) for a group of dimensions."""
        return cfl_exclude.build_group_distinct_select(
            dims, model, graph, qualify, via_constraints=via_constraints
        )

    def _build_existing_pairs_select(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
    ) -> Select:
        """Build SELECT for existing dimension combinations via fact-table joins."""
        return cfl_exclude.build_existing_pairs_select(self, resolved, model, graph, qualify)
