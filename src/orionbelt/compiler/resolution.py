"""Phase 1: Resolve semantic references to physical expressions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from orionbelt.ast.nodes import (
    BinaryOp,
    CaseExpr,
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    OrderByItem,
    UnaryOp,
)
from orionbelt.compiler.expr_parser import (
    parse_expression,
    tokenize_measure_expression,
    tokenize_metric_formula,
)
from orionbelt.compiler.filters import (
    build_filter_expr,
    build_measure_filter_condition,
    collect_measure_filter_objects,
)
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    CoalesceDimension,
    DimensionRef,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    UsePathName,
)
from orionbelt.models.semantic import (
    CumulativeAggType,
    FilterContext,
    GrainMode,
    GrainOverride,
    GrainToDate,
    Measure,
    Metric,
    MetricType,
    ModelFilter,
    PeriodOverPeriodComparison,
    SemanticModel,
    TimeGrain,
)


@dataclass
class ResolvedDimension:
    """A resolved dimension with its physical column reference."""

    name: str
    object_name: str
    column_name: str
    source_column: str
    grain: TimeGrain | None = None
    via: str | None = None  # Role-playing waypoint (data object the join must traverse)
    coalesce_alias: str | None = None  # Set when this dim is part of a coalesce group


@dataclass
class ResolvedMeasure:
    """A resolved measure with its aggregate expression."""

    name: str
    aggregation: str
    expression: Expr
    is_expression: bool = False
    component_measures: list[str] = field(default_factory=list)
    total: bool = False
    # Grain override fields
    grain_override: GrainOverride | None = None
    effective_grain: list[str] | None = None
    # Filter context fields
    filter_context: FilterContext | None = None
    # Cumulative metric fields
    is_cumulative: bool = False
    cumulative_measure: str | None = None
    cumulative_time_dimension: str | None = None
    cumulative_type: CumulativeAggType = CumulativeAggType.SUM
    cumulative_window: int | None = None
    cumulative_grain_to_date: GrainToDate | None = None
    # Period-over-period metric fields
    is_pop: bool = False
    pop_base_measure: str | None = None
    pop_time_dimension: str | None = None
    pop_grain: TimeGrain | None = None
    pop_offset: int = -1
    pop_offset_grain: TimeGrain | None = None
    pop_comparison: PeriodOverPeriodComparison | None = None


@dataclass
class ResolvedFilter:
    """A resolved filter with physical expression."""

    expression: Expr
    is_aggregate: bool = False
    referenced_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass
class ResolvedQuery:
    """Result of query resolution — ready for SQL planning."""

    dimensions: list[ResolvedDimension] = field(default_factory=list)
    measures: list[ResolvedMeasure] = field(default_factory=list)
    base_object: str = ""
    required_objects: set[str] = field(default_factory=set)
    join_steps: list[JoinStep] = field(default_factory=list)
    where_filters: list[ResolvedFilter] = field(default_factory=list)
    having_filters: list[ResolvedFilter] = field(default_factory=list)
    order_by_exprs: list[tuple[Expr, bool]] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    warnings: list[str] = field(default_factory=list)
    requires_cfl: bool = False
    measure_source_objects: set[str] = field(default_factory=set)
    metric_components: dict[str, ResolvedMeasure] = field(default_factory=dict)
    use_path_names: list[UsePathName] = field(default_factory=list)
    via_constraints: dict[str, str] = field(default_factory=dict)
    dimensions_exclude: bool = False
    coalesce_aliases: set[str] = field(default_factory=set)

    @property
    def fact_tables(self) -> list[str]:
        if self.measure_source_objects:
            return sorted(self.measure_source_objects)
        return [self.base_object] if self.base_object else []

    @property
    def has_totals(self) -> bool:
        """Check if any measure (direct or metric component) uses total or grain override."""
        for m in self.measures:
            if m.total or m.grain_override is not None:
                return True
            for comp_name in m.component_measures:
                comp = self.metric_components.get(comp_name)
                if comp and (comp.total or comp.grain_override is not None):
                    return True
        return False

    @property
    def has_grain_overrides(self) -> bool:
        """Check if any measure (direct or metric component) uses grain override."""
        for m in self.measures:
            if m.grain_override is not None:
                return True
            for comp_name in m.component_measures:
                comp = self.metric_components.get(comp_name)
                if comp and comp.grain_override is not None:
                    return True
        return False

    @property
    def has_filter_context(self) -> bool:
        """Check if any measure has a filter context override."""
        return any(m.filter_context is not None for m in self.measures)

    @property
    def has_cumulative(self) -> bool:
        """Check if any selected metric is cumulative."""
        return any(m.is_cumulative for m in self.measures)

    @property
    def has_pop(self) -> bool:
        """Check if any selected metric is period-over-period."""
        return any(m.is_pop for m in self.measures)


@dataclass
class _ResolutionContext:
    """Mutable state accumulated during query resolution."""

    model: SemanticModel
    errors: list[SemanticError] = field(default_factory=list)
    global_columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    result: ResolvedQuery = field(default_factory=ResolvedQuery)
    joined_objects: set[str] = field(default_factory=set)
    graph: JoinGraph | None = None


def _resolve_effective_grain(grain: GrainOverride, query_dims: list[str]) -> list[str]:
    """Compute the effective grain dimensions for a measure grain override."""
    if grain.mode == GrainMode.FIXED:
        if grain.keep_only:
            return [d for d in grain.keep_only if d in query_dims]
        return list(grain.include)
    # RELATIVE mode
    result = [d for d in query_dims if d not in grain.exclude]
    result.extend(d for d in grain.include if d not in result)
    return result


class QueryResolver:
    """Resolves a QueryObject + SemanticModel into a ResolvedQuery."""

    def resolve(self, query: QueryObject, model: SemanticModel) -> ResolvedQuery:
        ctx = _ResolutionContext(
            model=model,
            result=ResolvedQuery(
                limit=query.limit,
                offset=query.offset,
                use_path_names=list(query.use_path_names),
            ),
        )

        # Build global column lookup: col_name → (object_name, source_column)
        for obj_name, obj in model.data_objects.items():
            for col_name, col_obj in obj.columns.items():
                ctx.global_columns[col_name] = (obj_name, col_obj.code)

        # 1. Resolve dimensions (string or coalesce group).
        # Coalesce groups expand into their constituent dimensions, each
        # tagged with the same coalesce_alias so the CFL outer wrapper can
        # emit COALESCE(d1, d2, ...) AS <alias>.
        for dim_entry in query.select.dimensions:
            if isinstance(dim_entry, CoalesceDimension):
                self._resolve_coalesce_dimension(ctx, dim_entry, ctx.result.coalesce_aliases)
            else:
                self._append_resolved_dimension(ctx, dim_entry)

        # 2. Resolve measures and track their source objects
        for measure_name in query.select.measures:
            resolved_meas = self._resolve_measure(ctx, measure_name)
            if resolved_meas:
                ctx.result.measures.append(resolved_meas)
                source_objs = self._get_measure_source_objects(ctx, measure_name)
                ctx.result.measure_source_objects.update(source_objs)
                ctx.result.required_objects.update(source_objs)

        # 3. Determine base object (the one with most joins / most measures)
        ctx.result.base_object = self._select_base_object(ctx)
        if ctx.result.base_object:
            ctx.result.required_objects.add(ctx.result.base_object)

        # Detect multi-fact: CFL is needed only when measure source objects
        # span multiple independent fact tables.
        if len(ctx.result.measure_source_objects) > 1:
            graph = JoinGraph(model, use_path_names=query.use_path_names or None)
            reachable = graph.descendants(ctx.result.base_object)
            unreachable = ctx.result.measure_source_objects - reachable - {ctx.result.base_object}
            if unreachable:
                ctx.result.requires_cfl = True

        # Dimension-only queries: when dimensions span independent branches,
        # join through intermediate bridge/fact tables (no CFL needed).
        # Add intermediate tables from the join steps to required_objects
        # so the star schema planner includes them.
        if not ctx.result.measure_source_objects and ctx.result.dimensions:
            dim_objects = {d.object_name for d in ctx.result.dimensions}
            if not dim_objects <= {ctx.result.base_object}:
                graph = JoinGraph(model, use_path_names=query.use_path_names or None)
                steps = graph.find_join_path(
                    {ctx.result.base_object},
                    dim_objects,
                    via_constraints=ctx.result.via_constraints or None,
                )
                for step in steps:
                    ctx.result.required_objects.add(step.from_object)
                    ctx.result.required_objects.add(step.to_object)

        # Validate dimensionsExclude constraints
        if query.dimensions_exclude:
            if query.select.measures:
                ctx.errors.append(
                    SemanticError(
                        code="DIMENSIONS_EXCLUDE_WITH_MEASURES",
                        message="dimensionsExclude cannot be combined with measures",
                        path="select",
                    )
                )
            elif len(ctx.result.dimensions) < 2:
                ctx.errors.append(
                    SemanticError(
                        code="DIMENSIONS_EXCLUDE_INSUFFICIENT",
                        message="dimensionsExclude requires at least 2 dimensions",
                        path="select.dimensions",
                    )
                )
            else:
                ctx.result.dimensions_exclude = True

        # 4. Validate usePathNames before building join graph
        self._validate_use_path_names(ctx, query.use_path_names)

        # 5. Resolve join paths
        ctx.graph = JoinGraph(model, use_path_names=query.use_path_names or None)
        if ctx.result.base_object and len(ctx.result.required_objects) > 1:
            ctx.result.join_steps = ctx.graph.find_join_path(
                {ctx.result.base_object},
                ctx.result.required_objects,
                via_constraints=ctx.result.via_constraints or None,
            )

        # Build set of all objects present in the query's join graph
        if ctx.result.base_object:
            ctx.joined_objects.add(ctx.result.base_object)
        for step in ctx.result.join_steps:
            ctx.joined_objects.add(step.to_object)

        # Detect required objects that the star-schema planner cannot reach.
        # Many-to-one joins are forward-only (reverse traversal would inflate
        # the base table), so a required object that's only reachable via a
        # reverse m-to-1 hop is unreachable.  Raise a clear error rather than
        # silently producing wrong SQL.  CFL legs are validated separately.
        if ctx.result.base_object and not ctx.result.requires_cfl:
            unreachable = ctx.result.required_objects - ctx.joined_objects
            for unreachable_name in sorted(unreachable):
                ctx.errors.append(
                    SemanticError(
                        code="UNREACHABLE_REQUIRED_OBJECT",
                        message=(
                            f"Data object '{unreachable_name}' is required by the query but "
                            f"cannot be reached from base '{ctx.result.base_object}' via "
                            f"directed joins. Many-to-one joins are forward-only; reverse "
                            f"traversal would inflate row counts. Add an explicit join from "
                            f"'{ctx.result.base_object}' (or an intermediate object) to "
                            f"'{unreachable_name}', or split the query so each fact is "
                            f"queried independently."
                        ),
                        path="select",
                    )
                )

        # 5b. Inject static model filters — always applied as WHERE conditions
        static_exprs: list[Expr] = []
        for mf in model.filters:
            static_filter = self._resolve_static_filter(ctx, mf)
            if static_filter:
                ctx.result.where_filters.append(static_filter)
                static_exprs.append(static_filter.expression)

        # 6. Classify filters — skip query-time duplicates of static filters
        for qfi in query.where:
            resolved_filter = self._resolve_filter_item(ctx, qfi, is_having=False)
            if resolved_filter and resolved_filter.expression not in static_exprs:
                ctx.result.where_filters.append(resolved_filter)

        for qfi in query.having:
            resolved_filter = self._resolve_filter_item(ctx, qfi, is_having=True)
            if resolved_filter:
                ctx.result.having_filters.append(resolved_filter)

        # 7. Resolve order by — must reference a dimension or measure in SELECT
        select_count = len(ctx.result.dimensions) + len(ctx.result.measures)
        for ob in query.order_by:
            expr = self._resolve_order_by_field(ctx, ob.field, select_count)
            if expr:
                ctx.result.order_by_exprs.append((expr, ob.direction == "desc"))

        if ctx.errors:
            raise ResolutionError(ctx.errors)

        return ctx.result

    # -- dimensions ----------------------------------------------------------

    def _append_resolved_dimension(
        self,
        ctx: _ResolutionContext,
        dim_str: str,
        coalesce_alias: str | None = None,
    ) -> ResolvedDimension | None:
        """Resolve a single dimension string and append it to the result."""
        dim_ref = DimensionRef.parse(dim_str)
        resolved_dim = self._resolve_dimension(ctx, dim_ref)
        if resolved_dim is None:
            return None
        dim_def = ctx.model.dimensions.get(dim_ref.name)
        if dim_def and dim_def.via:
            resolved_dim.via = dim_def.via
            ctx.result.required_objects.add(dim_def.via)
            ctx.result.via_constraints[resolved_dim.object_name] = dim_def.via
        if coalesce_alias is not None:
            resolved_dim.coalesce_alias = coalesce_alias
        ctx.result.dimensions.append(resolved_dim)
        ctx.result.required_objects.add(resolved_dim.object_name)
        return resolved_dim

    def _resolve_coalesce_dimension(
        self,
        ctx: _ResolutionContext,
        coalesce: CoalesceDimension,
        seen_aliases: set[str],
    ) -> None:
        """Expand a coalesce group into its constituent resolved dimensions.

        Validates: at least 2 members, alias is unique within the query and
        does not collide with an existing dimension/measure name, all members
        resolve to the same abstract column type.
        """
        alias = coalesce.alias
        if not alias:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_MISSING_ALIAS",
                    message="Coalesce dimension requires a non-empty 'as' alias",
                    path="select.dimensions",
                )
            )
            return
        if alias in seen_aliases:
            ctx.errors.append(
                SemanticError(
                    code="DUPLICATE_COALESCE_ALIAS",
                    message=f"Duplicate coalesce alias '{alias}' in this query",
                    path="select.dimensions",
                )
            )
            return
        if alias in ctx.model.dimensions or alias in ctx.model.measures:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_ALIAS_COLLISION",
                    message=(
                        f"Coalesce alias '{alias}' collides with an existing "
                        f"model dimension or measure name"
                    ),
                    path="select.dimensions",
                )
            )
            return
        if len(coalesce.coalesce) < 2:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_TOO_FEW_MEMBERS",
                    message=(
                        f"Coalesce '{alias}' requires at least 2 dimensions "
                        f"(got {len(coalesce.coalesce)})"
                    ),
                    path="select.dimensions",
                )
            )
            return
        seen_aliases.add(alias)

        # Resolve each member with the alias tag; verify type compatibility.
        member_types: set[str] = set()
        for member in coalesce.coalesce:
            resolved = self._append_resolved_dimension(ctx, member, coalesce_alias=alias)
            if resolved:
                dim_def = ctx.model.dimensions.get(member)
                if dim_def:
                    member_types.add(dim_def.result_type.value)
        if len(member_types) > 1:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_TYPE_MISMATCH",
                    message=(
                        f"Coalesce '{alias}' members have incompatible result types: "
                        f"{sorted(member_types)}"
                    ),
                    path="select.dimensions",
                )
            )

    def _resolve_dimension(
        self, ctx: _ResolutionContext, ref: DimensionRef
    ) -> ResolvedDimension | None:
        """Resolve a dimension reference to its physical column."""
        dim = ctx.model.dimensions.get(ref.name)
        if dim is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_DIMENSION",
                    message=f"Unknown dimension '{ref.name}'",
                    path="select.dimensions",
                )
            )
            return None

        obj_name = dim.view
        col_name = dim.column
        obj = ctx.model.data_objects.get(obj_name)
        if obj is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_DATA_OBJECT",
                    message=f"Dimension '{ref.name}' references unknown data object '{obj_name}'",
                )
            )
            return None

        vf = obj.columns.get(col_name)
        source_col = vf.code if vf else col_name

        return ResolvedDimension(
            name=ref.name,
            object_name=obj_name,
            column_name=col_name,
            source_column=source_col,
            grain=ref.grain or dim.time_grain,
        )

    # -- measures & metrics --------------------------------------------------

    def _resolve_measure(self, ctx: _ResolutionContext, name: str) -> ResolvedMeasure | None:
        """Resolve a measure name to its aggregate expression."""
        measure = ctx.model.measures.get(name)
        if measure is None:
            metric = ctx.model.metrics.get(name)
            if metric:
                return self._resolve_metric(ctx, name, metric)
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_MEASURE",
                    message=f"Unknown measure '{name}'",
                    path="select.measures",
                )
            )
            return None

        expr = self._build_measure_expr(ctx, measure)
        grain_override = measure.grain
        effective_grain: list[str] | None = None
        if grain_override is not None:
            query_dim_names = [d.name for d in ctx.result.dimensions]
            effective_grain = _resolve_effective_grain(grain_override, query_dim_names)
            if effective_grain is not None and not set(effective_grain) <= set(query_dim_names):
                bad = sorted(set(effective_grain) - set(query_dim_names))
                ctx.errors.append(
                    SemanticError(
                        code="GRAIN_NOT_SUBSET",
                        message=(
                            f"Measure '{name}' grain {bad} is not a subset of "
                            f"query dimensions {query_dim_names}. "
                            f"This would cause row multiplication."
                        ),
                        path="select.measures",
                    )
                )
        return ResolvedMeasure(
            name=name,
            aggregation=measure.aggregation,
            expression=expr,
            is_expression=measure.expression is not None,
            total=measure.total,
            grain_override=grain_override,
            effective_grain=effective_grain,
            filter_context=measure.filter_context,
        )

    def _build_measure_expr(self, ctx: _ResolutionContext, measure: Measure) -> Expr:
        """Build the aggregate expression for a measure."""
        if measure.expression:
            return self._expand_expression(ctx, measure)

        # Build column references for all columns
        args: list[Expr] = []
        if measure.columns:
            for ref in measure.columns:
                obj_name = ref.view or ""
                col_name = ref.column or ""
                obj = ctx.model.data_objects.get(obj_name)
                source = obj.columns[col_name].code if obj and col_name in obj.columns else col_name
                args.append(ColumnRef(name=source, table=obj_name))
        if not args:
            args = [Literal.number(1)]

        agg = measure.aggregation.upper()
        distinct = measure.distinct
        if agg == "COUNT_DISTINCT":
            agg = "COUNT"
            distinct = True

        # LISTAGG: attach separator and optional ordering
        separator: str | None = None
        order_by: list[OrderByItem] = []
        if agg == "LISTAGG":
            separator = measure.delimiter if measure.delimiter is not None else ","
            if measure.within_group:
                wg = measure.within_group
                wg_obj_name = wg.column.view or ""
                wg_col_name = wg.column.column or ""
                wg_obj = ctx.model.data_objects.get(wg_obj_name)
                wg_source = (
                    wg_obj.columns[wg_col_name].code
                    if wg_obj and wg_col_name in wg_obj.columns
                    else wg_col_name
                )
                order_by = [
                    OrderByItem(
                        expr=ColumnRef(name=wg_source, table=wg_obj_name),
                        desc=wg.order.upper() == "DESC",
                    )
                ]

        result = FunctionCall(
            name=agg,
            args=args,
            distinct=distinct,
            order_by=order_by,
            separator=separator,
        )
        return self._apply_measure_filters(ctx, measure, result)

    def _expand_expression(self, ctx: _ResolutionContext, measure: Measure) -> Expr:
        """Expand a measure expression with ``{[DataObject].[Column]}`` refs into AST."""
        formula = measure.expression or ""
        agg = measure.aggregation.upper()

        tokens = tokenize_measure_expression(formula, ctx.model)
        inner = parse_expression(tokens)

        distinct = measure.distinct
        if agg == "COUNT_DISTINCT":
            agg = "COUNT"
            distinct = True

        result = FunctionCall(
            name=agg,
            args=[inner],
            distinct=distinct,
        )
        return self._apply_measure_filters(ctx, measure, result)

    @staticmethod
    def _apply_measure_filters(
        ctx: _ResolutionContext, measure: Measure, func: FunctionCall
    ) -> FunctionCall:
        """Wrap aggregate args with CASE WHEN if the measure has filters."""
        if not measure.filters:
            return func
        condition = build_measure_filter_condition(measure.filters, ctx.model, ctx.errors)
        if condition is None:
            return func
        wrapped_args: list[Expr] = [CaseExpr(when_clauses=[(condition, arg)]) for arg in func.args]
        return FunctionCall(
            name=func.name,
            args=wrapped_args,
            distinct=func.distinct,
            order_by=func.order_by,
            separator=func.separator,
        )

    def _resolve_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a metric to its combined expression."""
        if metric.type == MetricType.CUMULATIVE:
            return self._resolve_cumulative_metric(ctx, name, metric)
        if metric.type == MetricType.PERIOD_OVER_PERIOD:
            return self._resolve_pop_metric(ctx, name, metric)
        return self._resolve_derived_metric(ctx, name, metric)

    def _resolve_derived_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a derived metric to its combined expression."""
        formula = metric.expression

        # Extract and resolve each component measure
        component_names = re.findall(r"\{\[([^\]]+)\]\}", formula or "")
        for comp_name in component_names:
            if comp_name not in ctx.result.metric_components:
                comp = self._resolve_measure(ctx, comp_name)
                if comp:
                    ctx.result.metric_components[comp_name] = comp

        # Parse the formula into an AST tree
        try:
            tokens = tokenize_metric_formula(formula or "")
            parsed_expr = parse_expression(tokens)
        except Exception as exc:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_METRIC_EXPRESSION",
                    message=f"Metric '{name}' has invalid expression: {exc}",
                    path=f"metrics.{name}.expression",
                )
            )
            return None

        return ResolvedMeasure(
            name=name,
            aggregation="",
            expression=parsed_expr,
            component_measures=component_names,
            is_expression=True,
        )

    def _resolve_cumulative_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a cumulative metric referencing an existing measure."""
        if metric.measure is None:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_METRIC",
                    message=f"Cumulative metric '{name}' missing required 'measure' field",
                    path=f"metrics.{name}",
                )
            )
            return None
        if metric.time_dimension is None:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_METRIC",
                    message=f"Cumulative metric '{name}' missing required 'timeDimension' field",
                    path=f"metrics.{name}",
                )
            )
            return None

        # Validate referenced measure exists
        base_measure = ctx.model.measures.get(metric.measure)
        if base_measure is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_MEASURE",
                    message=(
                        f"Cumulative metric '{name}' references unknown measure '{metric.measure}'"
                    ),
                    path=f"metrics.{name}.measure",
                )
            )
            return None

        # Validate timeDimension is a known dimension
        dim = ctx.model.dimensions.get(metric.time_dimension)
        if dim is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_DIMENSION",
                    message=(
                        f"Cumulative metric '{name}' references unknown "
                        f"timeDimension '{metric.time_dimension}'"
                    ),
                    path=f"metrics.{name}.timeDimension",
                )
            )
            return None

        # Validate timeDimension is in the query's selected dimensions
        dim_names = {d.name for d in ctx.result.dimensions}
        if metric.time_dimension not in dim_names:
            ctx.errors.append(
                SemanticError(
                    code="CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT",
                    message=(
                        f"Cumulative metric '{name}' requires timeDimension "
                        f"'{metric.time_dimension}' to be in the query's selected dimensions"
                    ),
                    path=f"metrics.{name}.timeDimension",
                )
            )
            return None

        # Resolve the base measure as a component (reuse existing resolution)
        if metric.measure not in ctx.result.metric_components:
            comp = self._resolve_measure(ctx, metric.measure)
            if comp:
                ctx.result.metric_components[metric.measure] = comp

        # The cumulative metric's expression is a placeholder ColumnRef to the base measure
        # The actual window function is built during the cumulative_wrap phase
        return ResolvedMeasure(
            name=name,
            aggregation=base_measure.aggregation,
            expression=ColumnRef(name=metric.measure),
            is_expression=True,
            component_measures=[metric.measure],
            is_cumulative=True,
            cumulative_measure=metric.measure,
            cumulative_time_dimension=metric.time_dimension,
            cumulative_type=metric.cumulative_type,
            cumulative_window=metric.window,
            cumulative_grain_to_date=metric.grain_to_date,
        )

    def _resolve_pop_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a period-over-period metric."""
        if metric.period_over_period is None:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_METRIC",
                    message=f"PoP metric '{name}' missing required 'periodOverPeriod' field",
                    path=f"metrics.{name}",
                )
            )
            return None
        if metric.expression is None:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_METRIC",
                    message=f"PoP metric '{name}' missing required 'expression' field",
                    path=f"metrics.{name}",
                )
            )
            return None

        pop = metric.period_over_period

        # Validate timeDimension is a known dimension
        dim = ctx.model.dimensions.get(pop.time_dimension)
        if dim is None:
            ctx.errors.append(
                SemanticError(
                    code="POP_UNKNOWN_TIME_DIMENSION",
                    message=(
                        f"Period-over-period metric '{name}' references unknown "
                        f"time dimension '{pop.time_dimension}'"
                    ),
                    path=f"metrics.{name}.periodOverPeriod.timeDimension",
                )
            )
            return None

        # Validate timeDimension is in the query's selected dimensions
        dim_names = {d.name for d in ctx.result.dimensions}
        if pop.time_dimension not in dim_names:
            ctx.errors.append(
                SemanticError(
                    code="POP_TIME_DIMENSION_NOT_IN_SELECT",
                    message=(
                        f"Period-over-period metric '{name}' requires time dimension "
                        f"'{pop.time_dimension}' to be in the query's selected dimensions"
                    ),
                    path=f"metrics.{name}.periodOverPeriod.timeDimension",
                )
            )
            return None

        # Validate offset is non-zero
        if pop.offset == 0:
            ctx.errors.append(
                SemanticError(
                    code="POP_INVALID_OFFSET",
                    message=(
                        f"Period-over-period metric '{name}' has offset=0 "
                        f"(must be non-zero, e.g. -1 for previous period)"
                    ),
                    path=f"metrics.{name}.periodOverPeriod.offset",
                )
            )
            return None

        # Resolve the expression (same as derived — parse {[Measure Name]} refs)
        component_names = re.findall(r"\{\[([^\]]+)\]\}", metric.expression)

        # PoP comparison logic only supports single-measure expressions
        if len(component_names) > 1:
            ctx.errors.append(
                SemanticError(
                    code="POP_MULTI_MEASURE_NOT_SUPPORTED",
                    message=(
                        f"Period-over-period metric '{name}' references multiple measures "
                        f"({', '.join(component_names)}). PoP comparison currently supports "
                        f"only single-measure expressions."
                    ),
                    path=f"metrics.{name}.expression",
                )
            )
            return None

        for comp_name in component_names:
            if comp_name not in ctx.result.metric_components:
                comp = self._resolve_measure(ctx, comp_name)
                if comp:
                    ctx.result.metric_components[comp_name] = comp

        try:
            tokens = tokenize_metric_formula(metric.expression)
            parsed_expr = parse_expression(tokens)
        except Exception as exc:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_METRIC_EXPRESSION",
                    message=f"Metric '{name}' has invalid expression: {exc}",
                    path=f"metrics.{name}.expression",
                )
            )
            return None

        # Use the first component measure as the base (for single-measure PoP)
        pop_base = component_names[0] if component_names else None

        return ResolvedMeasure(
            name=name,
            aggregation="",
            expression=parsed_expr,
            component_measures=component_names,
            is_expression=True,
            is_pop=True,
            pop_base_measure=pop_base,
            pop_time_dimension=pop.time_dimension,
            pop_grain=pop.grain,
            pop_offset=pop.offset,
            pop_offset_grain=pop.offset_grain,
            pop_comparison=pop.comparison,
        )

    def _get_measure_source_objects(self, ctx: _ResolutionContext, name: str) -> set[str]:
        """Extract all source data objects for a measure or metric."""
        result: set[str] = set()

        measure = ctx.model.measures.get(name)
        if measure:
            for cref in measure.columns:
                if cref.view:
                    result.add(cref.view)
            if measure.expression:
                col_refs = re.findall(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}", measure.expression)
                for obj_name, _col_name in col_refs:
                    result.add(obj_name)
            for fi in measure.filters:
                collect_measure_filter_objects(fi, result)
            return result

        metric = ctx.model.metrics.get(name)
        if metric:
            if metric.type == MetricType.CUMULATIVE and metric.measure:
                # Cumulative metric: source objects come from the referenced measure
                result.update(self._get_measure_source_objects(ctx, metric.measure))
            elif metric.expression:
                # Derived or PoP metric: parse expression for measure references
                measure_refs = re.findall(r"\{\[([^\]]+)\]\}", metric.expression)
                for ref_name in measure_refs:
                    result.update(self._get_measure_source_objects(ctx, ref_name))

        return result

    # -- base object selection -----------------------------------------------

    def _select_base_object(self, ctx: _ResolutionContext) -> str:
        """Select the base (fact) object — prefer measure source objects with most joins."""
        if ctx.result.measure_source_objects:
            best = ""
            best_joins = -1
            for obj_name in sorted(ctx.result.measure_source_objects):
                obj = ctx.model.data_objects.get(obj_name)
                n = len(obj.joins) if obj else 0
                if n > best_joins:
                    best = obj_name
                    best_joins = n
            if best:
                return best

        # Dimension-only: use JoinGraph to find the deepest ancestor
        # (possibly an intermediate fact/bridge table) that can reach
        # all required dimension objects via directed join paths.
        if len(ctx.result.required_objects) > 1:
            graph = JoinGraph(ctx.model, use_path_names=ctx.result.use_path_names or None)
            root = graph.find_common_root(ctx.result.required_objects)
            if root:
                return root

        for obj_name in sorted(ctx.result.required_objects):
            obj = ctx.model.data_objects.get(obj_name)
            if obj and obj.joins:
                return obj_name

        if ctx.result.required_objects:
            return next(iter(sorted(ctx.result.required_objects)))
        if ctx.model.data_objects:
            return next(iter(ctx.model.data_objects))
        return ""

    # -- usePathNames validation ---------------------------------------------

    def _validate_use_path_names(
        self, ctx: _ResolutionContext, use_path_names: list[UsePathName]
    ) -> None:
        """Validate usePathNames references."""
        for upn in use_path_names:
            if upn.source not in ctx.model.data_objects:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT",
                        message=f"usePathNames references unknown data object '{upn.source}'",
                        path="usePathNames",
                    )
                )
                continue
            if upn.target not in ctx.model.data_objects:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT",
                        message=f"usePathNames references unknown data object '{upn.target}'",
                        path="usePathNames",
                    )
                )
                continue
            source_obj = ctx.model.data_objects[upn.source]
            found = any(
                j.join_to == upn.target and j.secondary and j.path_name == upn.path_name
                for j in source_obj.joins
            )
            if not found:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_PATH_NAME",
                        message=(
                            f"No secondary join with pathName '{upn.path_name}' "
                            f"from '{upn.source}' to '{upn.target}'"
                        ),
                        path="usePathNames",
                    )
                )

    # -- static model filters ------------------------------------------------

    def _resolve_static_filter(
        self, ctx: _ResolutionContext, mf: ModelFilter
    ) -> ResolvedFilter | None:
        """Resolve a static model filter to a physical WHERE expression.

        Silently skips filters on data objects that are unreachable from the
        query's join graph — they are simply irrelevant to the current query.
        """
        obj = ctx.model.data_objects.get(mf.data_object)
        if obj is None:
            return None

        col = obj.columns.get(mf.column)
        if col is None:
            return None

        if not self._resolve_filter_object(ctx, mf.data_object, "filters", mf.column):
            return None

        col_expr: Expr = ColumnRef(name=col.code, table=mf.data_object)
        qf = QueryFilter(field=mf.column, op=mf.operator, value=mf.value or mf.values or None)
        filter_expr = build_filter_expr(col_expr, qf, ctx.errors)
        if filter_expr is None:
            return None
        return ResolvedFilter(
            expression=filter_expr,
            is_aggregate=False,
            referenced_fields=frozenset({mf.column}),
        )

    # -- filters -------------------------------------------------------------

    def _resolve_filter_object(
        self,
        ctx: _ResolutionContext,
        obj_name: str,
        filter_path: str,
        field_label: str,
    ) -> bool:
        """Ensure *obj_name* is joined; auto-extend if reachable.

        Silently skips filters on unreachable data objects — they are
        irrelevant to the current query.
        """
        if obj_name in ctx.joined_objects:
            return True
        if ctx.graph is None:
            return False
        reachable = any(obj_name in ctx.graph.descendants(j) for j in list(ctx.joined_objects))
        if not reachable:
            return False
        new_steps = ctx.graph.find_join_path(ctx.joined_objects, {obj_name})
        for step in new_steps:
            if step.to_object not in ctx.joined_objects:
                ctx.result.join_steps.append(step)
                ctx.joined_objects.add(step.to_object)
                ctx.result.required_objects.add(step.to_object)
        return True

    def _resolve_filter_item(
        self, ctx: _ResolutionContext, item: QueryFilterItem, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a filter item (leaf or group) to a physical expression."""
        if isinstance(item, QueryFilter):
            return self._resolve_filter(ctx, item, is_having=is_having)
        return self._resolve_filter_group(ctx, item, is_having=is_having)

    def _resolve_filter_group(
        self, ctx: _ResolutionContext, group: QueryFilterGroup, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a filter group recursively, combining with AND/OR."""
        child_exprs: list[Expr] = []
        all_fields: set[str] = set()
        for child in group.filters:
            resolved = self._resolve_filter_item(ctx, child, is_having=is_having)
            if resolved:
                child_exprs.append(resolved.expression)
                all_fields.update(resolved.referenced_fields)

        if not child_exprs:
            return None

        # Combine children with the group's logic
        op = "AND" if group.logic == "and" else "OR"
        combined: Expr = child_exprs[0]
        for expr in child_exprs[1:]:
            combined = BinaryOp(left=combined, op=op, right=expr)

        # Optionally negate
        if group.negated:
            combined = UnaryOp(op="NOT", operand=combined)

        return ResolvedFilter(
            expression=combined,
            is_aggregate=is_having,
            referenced_fields=frozenset(all_fields),
        )

    def _resolve_filter(
        self, ctx: _ResolutionContext, qf: QueryFilter, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a query filter to a physical expression.

        Filter fields can reference:
        1. A dimension name (e.g. ``"Order Priority"``)
        2. A qualified column ``"DataObject.Column"`` (e.g. ``"Orders.Order Priority"``)
        3. For HAVING filters, a measure name (e.g. ``"Revenue"``)

        If the referenced data object is reachable but not yet joined, the
        join path is auto-extended.
        """
        filter_path = "having" if is_having else "where"

        # 1. Try dimension name
        dim = ctx.model.dimensions.get(qf.field)
        if dim:
            obj_name = dim.view
            if not self._resolve_filter_object(ctx, obj_name, filter_path, qf.field):
                return None
            col_name = dim.column
            obj = ctx.model.data_objects.get(obj_name)
            source = obj.columns[col_name].code if obj and col_name in obj.columns else col_name
            col_expr: Expr = ColumnRef(name=source, table=obj_name)

        # 2. HAVING: try measure or metric name
        elif is_having and (qf.field in ctx.model.measures or qf.field in ctx.model.metrics):
            col_expr = ColumnRef(name=qf.field)

        # 3. Try qualified column: "DataObject.Column"
        elif "." in qf.field:
            parts = qf.field.split(".", 1)
            obj_name, col_name = parts[0].strip(), parts[1].strip()
            obj = ctx.model.data_objects.get(obj_name)
            if obj is None:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_FIELD",
                        message=(f"Unknown data object '{obj_name}' in filter field '{qf.field}'"),
                        path=filter_path,
                    )
                )
                return None
            if col_name not in obj.columns:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_FIELD",
                        message=(
                            f"Unknown column '{col_name}' in data object "
                            f"'{obj_name}' for filter field '{qf.field}'"
                        ),
                        path=filter_path,
                    )
                )
                return None
            if not self._resolve_filter_object(ctx, obj_name, filter_path, qf.field):
                return None
            source = obj.columns[col_name].code
            col_expr = ColumnRef(name=source, table=obj_name)

        else:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=f"Unknown filter field '{qf.field}'",
                    path=filter_path,
                )
            )
            return None

        filter_expr = build_filter_expr(col_expr, qf, ctx.errors)
        if filter_expr is None:
            return None
        return ResolvedFilter(
            expression=filter_expr,
            is_aggregate=is_having,
            referenced_fields=frozenset({qf.field}),
        )

    # -- order by ------------------------------------------------------------

    def _resolve_order_by_field(
        self, ctx: _ResolutionContext, field_name: str, select_count: int
    ) -> Expr | None:
        """Resolve an order-by field to its expression."""
        # Coalesce alias: outer SELECT exposes it as a bare alias column,
        # so a table-less ColumnRef is the right form for both star and CFL.
        if field_name in ctx.result.coalesce_aliases:
            return ColumnRef(name=field_name)

        for dim in ctx.result.dimensions:
            if dim.name == field_name:
                return ColumnRef(name=dim.source_column, table=dim.object_name)

        for meas in ctx.result.measures:
            if meas.name == field_name:
                return meas.expression

        if field_name.isdigit():
            pos = int(field_name)
            if 1 <= pos <= select_count:
                return Literal.number(pos)
            ctx.errors.append(
                SemanticError(
                    code="INVALID_ORDER_BY_POSITION",
                    message=(
                        f"ORDER BY position {pos} is out of range "
                        f"(SELECT has {select_count} columns)"
                    ),
                    path="order_by",
                )
            )
            return None

        ctx.errors.append(
            SemanticError(
                code="UNKNOWN_ORDER_BY_FIELD",
                message=(
                    f"ORDER BY field '{field_name}' is not a dimension "
                    f"or measure in the query's SELECT"
                ),
                path="order_by",
            )
        )
        return None


class ResolutionError(Exception):
    """Raised when query resolution encounters errors."""

    def __init__(self, errors: list[SemanticError]) -> None:
        self.errors = errors
        messages = "; ".join(e.message for e in errors)
        super().__init__(f"Resolution errors: {messages}")
