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
            f"Measure '{measure_name}' uses aggregation '{aggregation}' which "
            "requires paired-row semantics and is not supported in CFL "
            "(multi-fact) queries. Restrict the query to a single fact "
            "table or restructure the model so this measure resolves via "
            "the star-schema path.",
        )


__all__ = ["CFLPlanner", "FanoutError", "UnsupportedAggregationForCFLError"]


def _expand_cfl_measure_refs(expr: Expr, measure_exprs: dict[str, Expr]) -> Expr:
    """Replace bare ColumnRef aliases in HAVING with their full aggregate expressions.

    Recurses through ``BinaryOp`` and ``FunctionCall.args`` so a metric
    formula like ``{Total Refunds} / NULLIF({Total Sales}, 0)`` correctly
    inlines both refs in HAVING / outer-SELECT contexts.
    """
    if isinstance(expr, ColumnRef) and expr.table is None and expr.name in measure_exprs:
        return measure_exprs[expr.name]
    if isinstance(expr, BinaryOp):
        new_left = _expand_cfl_measure_refs(expr.left, measure_exprs)
        new_right = _expand_cfl_measure_refs(expr.right, measure_exprs)
        if new_left is not expr.left or new_right is not expr.right:
            return BinaryOp(left=new_left, op=expr.op, right=new_right)
    if isinstance(expr, FunctionCall):
        new_args = [_expand_cfl_measure_refs(a, measure_exprs) for a in expr.args]
        if any(n is not o for n, o in zip(new_args, expr.args, strict=True)):
            return FunctionCall(
                name=expr.name,
                args=new_args,
                distinct=expr.distinct,
                order_by=expr.order_by,
                separator=expr.separator,
            )
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
        dialect: Dialect | None = None,
    ) -> str | None:
        """Resolve the SQL type for NULL padding in CFL UNION ALL legs.

        Two regimes apply:

        * **Numeric aggregates** (SUM / AVG / MIN / MAX / MEDIAN / etc.) —
          the inner column projection is the *aggregate's input column*, and
          OBSL casts the outer aggregate to the measure's declared
          ``dataType`` (e.g. ``decimal(18, 2)``). Padding with that same
          declared type keeps every CFL leg's column compatible with the
          outer ``SUM``/``AVG`` and avoids ClickHouse's ``Decimal`` +
          ``Float64`` Variant trap (where padding with the column's
          declared OBML ``abstractType: float`` mismatches storage as
          ``Decimal`` and produces ``ILLEGAL_TYPE_OF_ARGUMENT``).

        * **Count-style aggregates** (COUNT / COUNT_DISTINCT) — the inner
          column projection is the *raw column itself* (e.g. ``complid``,
          a text ID). The outer ``COUNT(DISTINCT ...)`` happily counts any
          type, but each CFL leg's column must agree on a type for
          ``UNION ALL``. Padding with the declared aggregate output type
          (BIGINT) trips strict-typed engines (Postgres / MySQL / strict
          ClickHouse) when the source column is text. Pad with the
          source column's abstract type instead.

        For multi-field measures (e.g. ``COUNT(a, b)``), per-column
        abstract types are used regardless of aggregation kind.
        """
        model_measure = model.measures.get(measure.name)
        if not model_measure:
            return None
        agg = (model_measure.aggregation or "").lower()
        is_count_style = agg in ("count", "count_distinct")
        # Multi-field measures: per-column abstract_type for each slot.
        if len(model_measure.columns) > 1:
            if field_idx < len(model_measure.columns):
                ref = model_measure.columns[field_idx]
                obj = model.data_objects.get(ref.view) if ref.view else None
                if obj and ref.column in obj.columns:
                    return obj.columns[ref.column].abstract_type.value
            return model_measure.result_type.value
        # Single-/zero-column COUNT-style: pad with the source column's
        # native type so UNION ALL legs agree (raw column, not aggregate).
        if is_count_style and len(model_measure.columns) == 1:
            ref = model_measure.columns[0]
            obj = model.data_objects.get(ref.view) if ref.view else None
            if obj and ref.column in obj.columns:
                return obj.columns[ref.column].abstract_type.value
        # Numeric aggregates: align padding with the outer CAST target.
        if dialect is not None and len(model_measure.columns) <= 1:
            resolved = resolve_measure_data_type(model_measure, model.settings)
            if resolved is not None:
                return dialect.render_obml_type(resolved)
        # Fallback to measure result_type.
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
        cte_name: str,
    ) -> Expr:
        """Build the outer query expression for a metric.

        Walks the metric's AST tree and replaces each ColumnRef(measure_name)
        with ``AGG("cte_name"."measure_name")`` using the component measure's
        aggregation. The CTE qualification matters: when the outer SELECT
        also aliases its column ``measure_name`` to ``AGG(...)``, ClickHouse
        resolves a bare ``"measure_name"`` to the sibling alias (the
        aggregate itself) and rejects the resulting nested aggregate as
        ``ILLEGAL_AGGREGATION``. Qualifying with the CTE name forces the
        inner ref to resolve to the raw CTE column.
        """
        return self._substitute_outer_refs(metric.expression, resolved, cte_name)

    def _substitute_outer_refs(self, expr: Expr, resolved: ResolvedQuery, cte_name: str) -> Expr:
        """Recursively substitute measure refs with outer aggregations.

        Walks ``BinaryOp`` and ``FunctionCall.args`` so a metric formula
        with embedded SQL functions (e.g. ``... / NULLIF(other, 0)``)
        substitutes refs inside the function call instead of leaving the
        bare label, which would later bind against a non-existent
        column.
        """
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
                    args=[ColumnRef(name=comp.name, table=cte_name)],
                    distinct=distinct,
                )
        if isinstance(expr, BinaryOp):
            new_left = self._substitute_outer_refs(expr.left, resolved, cte_name)
            new_right = self._substitute_outer_refs(expr.right, resolved, cte_name)
            if new_left is not expr.left or new_right is not expr.right:
                return BinaryOp(left=new_left, op=expr.op, right=new_right)
        if isinstance(expr, FunctionCall):
            new_args = [self._substitute_outer_refs(a, resolved, cte_name) for a in expr.args]
            if any(n is not o for n, o in zip(new_args, expr.args, strict=True)):
                return FunctionCall(
                    name=expr.name,
                    args=new_args,
                    distinct=expr.distinct,
                    order_by=expr.order_by,
                    separator=expr.separator,
                )
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
    def _remap_cfl_order_by(expr: Expr, resolved: ResolvedQuery, model: SemanticModel) -> Expr:
        """Remap ORDER BY expressions to use CTE aliases for the outer query.

        In CFL, the outer query selects from the composite CTE — original
        table-qualified refs are out of scope.  Remap dimension and measure
        expressions to their CTE alias names. Matches by structural equality
        with each dimension's column expression so computed columns (where
        the source AST is an inlined expression, not a bare ColumnRef) also
        remap correctly.
        """
        for dim in resolved.dimensions:
            if expr == make_column_expr(model, dim.object_name, dim.column_name):
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
        cte_name: str,
    ) -> Expr:
        """Build ``COUNT(DISTINCT CAST(f0 AS VARCHAR) || '|' || ...)`` for the outer query.

        Each field reference is qualified with *cte_name* so it resolves to
        the raw CTE column rather than any sibling SELECT alias (see
        ``_substitute_outer_refs`` for the alias-shadowing rationale).
        """
        parts: list[Expr] = [
            Cast(
                expr=ColumnRef(
                    name=self._multi_field_cte_alias(measure_name, i),
                    table=cte_name,
                ),
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
                agg_expr: Expr = self._build_outer_concat_count(
                    m.name, n_fields, agg, distinct, cte_name
                )
            else:
                agg_expr = FunctionCall(
                    name=agg,
                    args=[ColumnRef(name=m.name, table=cte_name)],
                    distinct=distinct,
                )
            # Apply CAST for resolved data_type
            model_measure = model.measures.get(m.name)
            if model_measure and dialect:
                resolved_type = resolve_measure_data_type(model_measure, settings)
                if resolved_type:
                    agg_expr = dialect.cast_to_obml_type(agg_expr, resolved_type)
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
            cte_query = self._build_group_distinct_select(
                group_dims,
                model,
                graph,
                qualify,
                via_constraints=resolved.via_constraints or None,
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
        via_constraints: dict[str, str] | None = None,
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
            col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
            builder.select(AliasedExpr(expr=col, alias=dim.name))
            builder.group_by(col)

        if root_obj:
            builder.from_(qualify(root_obj), alias=root)

        # Join to reach all dimension objects from root
        all_needed = required_objects | {root}
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
            col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
            builder.select(AliasedExpr(expr=col, alias=dim.name))
            builder.group_by(col)

        if best_fact_obj:
            builder.from_(qualify(best_fact_obj), alias=best_fact)

        # Required: all dimension objects + all fact tables
        all_needed = all_dim_objects | fact_tables | {best_fact}
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

        # Apply WHERE filters to existing pairs
        for wf in resolved.where_filters:
            builder.where(wf.expression)

        return builder.build()
