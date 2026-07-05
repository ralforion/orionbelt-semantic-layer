"""Phase 1: Resolve semantic references to physical expressions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from orionbelt.ast.nodes import (
    CaseExpr,
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    OrderByItem,
)
from orionbelt.compiler import (
    filter_resolution,
    metric_resolution,
    raw_resolution,
)
from orionbelt.compiler.expr_parser import (
    parse_expression,
    tokenize_measure_expression,
)
from orionbelt.compiler.filters import (
    build_measure_filter_condition,
    collect_measure_filter_objects,
)
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    CoalesceDimension,
    DimensionRef,
    Grouping,
    NullsPosition,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    UsePathName,
)
from orionbelt.models.semantic import (
    AggregationType,
    CumulativeAggType,
    DataObject,
    DataObjectColumn,
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
    WindowFunctionKind,
)

_COMPUTED_PLACEHOLDER = re.compile(r"\{(\w[^}]*)\}")


def _build_computed_column_expr(
    column: DataObjectColumn, obj: DataObject, model: SemanticModel
) -> Expr:
    """Parse a computed column's ``expression`` into an AST.

    ``{name}`` placeholders are substituted with ``{[obj.label].[name]}`` so
    the existing measure-expression tokenizer resolves them to physical
    table-qualified column refs. Falls back to a column ref to the column's
    own ``code`` if parsing fails — defensive: never block compilation on
    an expression-parse error in a single column.
    """
    expr_str = column.expression or ""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in obj.columns:
            return f"{{[{obj.label}].[{name}]}}"
        return match.group(0)

    rewritten = _COMPUTED_PLACEHOLDER.sub(_sub, expr_str)
    try:
        tokens = tokenize_measure_expression(rewritten, model)
        return parse_expression(tokens)
    except Exception:  # noqa: BLE001 — preserve previous behaviour on bad expression
        return ColumnRef(name=column.code or column.label, table=obj.label)


def make_column_expr(model: SemanticModel, object_name: str, column_label: str) -> Expr:
    """Build the AST expression that represents a column reference.

    For plain columns, returns ``ColumnRef(name=col.code, table=object_name)``.
    For computed columns (those with an ``expression``), parses and
    substitutes placeholders so the returned AST already inlines the
    expression. Used by planners and filter resolution alike — the
    single source of truth for "render this column reference as SQL".
    """
    obj = model.data_objects.get(object_name)
    if obj is None:
        return ColumnRef(name=column_label, table=object_name)
    column = obj.columns.get(column_label)
    if column is None:
        return ColumnRef(name=column_label, table=object_name)
    if column.expression:
        return _build_computed_column_expr(column, obj, model)
    return ColumnRef(name=column.code, table=object_name)


@dataclass
class ResolvedField:
    """A resolved raw-mode field reference: ``DataObject.Column`` → physical column.

    Raw mode (``select.fields``) bypasses the semantic dimension/measure layer
    and projects physical columns directly. The ``alias`` defaults to the
    original ``"DataObject.Column"`` reference so result columns are
    self-describing.
    """

    object_name: str  # logical data object name (table alias)
    column_name: str  # logical column name
    source_column: str  # physical column name in the source table
    alias: str  # output column name (defaults to "object_name.column_name")


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
    cumulative_partition_by: list[str] = field(default_factory=list)
    # Period-over-period metric fields
    is_pop: bool = False
    pop_base_measure: str | None = None
    pop_time_dimension: str | None = None
    pop_grain: TimeGrain | None = None
    pop_offset: int = -1
    pop_offset_grain: TimeGrain | None = None
    pop_comparison: PeriodOverPeriodComparison | None = None
    # Window metric fields (rank / lag / lead / ntile / first_value / last_value)
    is_window: bool = False
    window_function: WindowFunctionKind | None = None
    window_base_measure: str | None = None
    window_time_dimension: str | None = None
    window_partition_by: list[str] = field(default_factory=list)
    window_offset: int | None = None
    window_buckets: int | None = None
    window_order_direction: str = "desc"
    window_default_value: str | int | float | bool | None = None


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
    fields: list[ResolvedField] = field(default_factory=list)
    is_raw: bool = False
    distinct: bool = False
    base_object: str = ""
    required_objects: set[str] = field(default_factory=set)
    join_steps: list[JoinStep] = field(default_factory=list)
    where_filters: list[ResolvedFilter] = field(default_factory=list)
    having_filters: list[ResolvedFilter] = field(default_factory=list)
    order_by_exprs: list[tuple[Expr, bool, NullsPosition | None]] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    warnings: list[SemanticError] = field(default_factory=list)
    requires_cfl: bool = False
    measure_source_objects: set[str] = field(default_factory=set)
    metric_components: dict[str, ResolvedMeasure] = field(default_factory=dict)
    use_path_names: list[UsePathName] = field(default_factory=list)
    via_constraints: dict[str, str] = field(default_factory=dict)
    dimensions_exclude: bool = False
    coalesce_aliases: set[str] = field(default_factory=set)
    grouping: Grouping | None = None
    having_only_measures: set[str] = field(default_factory=set)
    """Measures auto-included by HAVING (not in ``select.measures``).

    Tracked so a future planner pass can optionally drop these from the
    final SELECT projection. Today they appear in output as an extra
    column, which keeps the SQL valid and the user gets a visual hint
    that the HAVING filter referenced an additional measure."""

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

    @property
    def has_window(self) -> bool:
        """Check if any selected metric is a window (rank/lag/lead/ntile/...)."""
        return any(m.is_window for m in self.measures)


@dataclass
class _ResolutionContext:
    """Mutable state accumulated during query resolution."""

    model: SemanticModel
    errors: list[SemanticError] = field(default_factory=list)
    global_columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    result: ResolvedQuery = field(default_factory=ResolvedQuery)
    joined_objects: set[str] = field(default_factory=set)
    graph: JoinGraph | None = None
    # Dialect-aware qualifier used by the EXISTS filter operator to render
    # its correlated subquery's FROM clause. ``None`` falls back to
    # ``obj.qualified_code`` (unquoted ``database.schema.code``), which is
    # only safe for engines that tolerate unquoted three-part identifiers
    # (e.g. DuckDB) — production code paths thread the dialect's
    # ``format_table_ref``.
    qualify_table: Callable[[DataObject], str] | None = None


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

    def resolve(
        self,
        query: QueryObject,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
    ) -> ResolvedQuery:
        ctx = _ResolutionContext(
            model=model,
            result=ResolvedQuery(
                limit=query.limit,
                offset=query.offset,
                use_path_names=list(query.use_path_names),
                is_raw=query.select.is_raw,
                distinct=query.select.distinct,
                grouping=query.grouping,
            ),
            qualify_table=qualify_table,
        )

        # Build global column lookup: col_name → (object_name, source_column)
        for obj_name, obj in model.data_objects.items():
            for col_name, col_obj in obj.columns.items():
                ctx.global_columns[col_name] = (obj_name, col_obj.code)

        if query.select.is_raw:
            # Raw mode: project physical columns, no aggregation.
            for ref in query.select.fields:
                self._resolve_raw_field(ctx, ref)
        else:
            # Aggregate mode (default).
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

            # 2.5. Auto-include measures referenced by HAVING but not by SELECT.
            # Without this, codegen emits a HAVING clause that references an
            # alias for a column the SELECT doesn't project — every database
            # rejects the SQL with a "must appear in GROUP BY" binder error.
            # Routing this through the regular measure-resolution path also
            # updates ``measure_source_objects`` so the multi-fact CFL trigger
            # below sees the HAVING-only measure's source.
            existing_measure_names = {m.name for m in ctx.result.measures}
            for ref in self._collect_having_measure_refs(query, model):
                if ref in existing_measure_names:
                    continue
                resolved_meas = self._resolve_measure(ctx, ref)
                if resolved_meas is None:
                    continue
                ctx.result.measures.append(resolved_meas)
                ctx.result.having_only_measures.add(ref)
                existing_measure_names.add(ref)
                source_objs = self._get_measure_source_objects(ctx, ref)
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

        # Raw mode: detect multi-fact (fields span objects unreachable from
        # the base via directed joins). The pipeline rejects this case for
        # now — raw CFL is a planned follow-up.
        if ctx.result.is_raw and ctx.result.base_object:
            field_objects = {f.object_name for f in ctx.result.fields}
            if len(field_objects) > 1:
                graph = JoinGraph(model, use_path_names=query.use_path_names or None)
                reachable = graph.descendants(ctx.result.base_object)
                unreachable = field_objects - reachable - {ctx.result.base_object}
                if unreachable:
                    ctx.result.requires_cfl = True

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
                ctx.result.order_by_exprs.append((expr, ob.direction == "desc", ob.nulls))

        # 8. ROLLUP / CUBE: backfill NULLS FIRST on any explicit ORDER BY entry
        # that didn't specify a NULLs position. Subtotal and grand-total rows
        # carry NULLs in the rolled-up group-by columns, and BI tools expect
        # those totals at the top of the result — not interleaved with details.
        if ctx.result.grouping is not None and ctx.result.order_by_exprs:
            ctx.result.order_by_exprs = [
                (expr, desc, NullsPosition.FIRST if nulls is None else nulls)
                for expr, desc, nulls in ctx.result.order_by_exprs
            ]

        # 9. Auto-order — when no explicit ORDER BY, append ORDER BY over all
        # SELECT dimensions (or raw fields) under two conditions:
        #   (a) LIMIT is set: cache hashes on compiled SQL; without ORDER BY
        #       ``LIMIT N`` returns any N rows, freezing one arbitrary slice.
        #   (b) ROLLUP / CUBE: subtotal layout is otherwise unpredictable.
        # ROLLUP / CUBE defaults to NULLS FIRST (totals at the top).
        # Aggregate-only queries (no dims, no fields) are already single-row
        # deterministic — skip.
        needs_auto_order = not ctx.result.order_by_exprs and (
            ctx.result.limit is not None or ctx.result.grouping is not None
        )
        if needs_auto_order:
            nulls_default = NullsPosition.FIRST if ctx.result.grouping is not None else None
            if ctx.result.is_raw and ctx.result.fields:
                for f in ctx.result.fields:
                    ctx.result.order_by_exprs.append(
                        (ColumnRef(name=f.alias), False, nulls_default)
                    )
            elif ctx.result.dimensions:
                for dim in ctx.result.dimensions:
                    ctx.result.order_by_exprs.append(
                        (ColumnRef(name=dim.name), False, nulls_default)
                    )

        if ctx.errors:
            raise ResolutionError(ctx.errors)

        return ctx.result

    # -- raw mode fields -----------------------------------------------------

    def _resolve_raw_field(self, ctx: _ResolutionContext, ref: str) -> None:
        """Resolve a ``DataObject.Column`` reference for raw-mode projection.

        Errors are accumulated in the resolution context (raised at the end).
        """
        raw_resolution.resolve_raw_field(self, ctx, ref)

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
        if alias in ctx.model.dimensions or alias in ctx.model.effective_measures:
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
        measure = ctx.model.effective_measures.get(name)
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
        # Engine-delegated aggregation (Databricks Metric View). Emit
        # ``MEASURE("<label>")`` literally — there's no source column
        # to read; the engine resolves the aggregation by name. Dialect
        # support is enforced downstream by ``_check_aggregation_supported``.
        if measure.aggregation == AggregationType.MEASURE:
            return FunctionCall(
                name="MEASURE",
                args=[ColumnRef(name=measure.label, table=None)],
            )
        if measure.expression:
            return self._expand_expression(ctx, measure)

        # Build column references for all columns. Routes through
        # ``make_column_expr`` so a measure column that points at a
        # computed (``expression:``) column inlines the template body
        # — without this, ``count_distinct`` over an ``expression:``
        # column would emit ``COUNT(DISTINCT "obj"."")`` (zero-length
        # identifier, DB error).
        args: list[Expr] = []
        if measure.columns:
            for ref in measure.columns:
                obj_name = ref.view or ""
                col_name = ref.column or ""
                # A column-less ref (``dataObject`` set, ``column`` empty) anchors the
                # measure on the object without naming a column — used by the
                # synthesized row-count measure to emit ``COUNT(*)`` while still
                # contributing the anchor to source-object resolution.
                if not col_name:
                    continue
                obj = ctx.model.data_objects.get(obj_name)
                if obj and col_name in obj.columns:
                    args.append(make_column_expr(ctx.model, obj_name, col_name))
                else:
                    args.append(ColumnRef(name=col_name, table=obj_name))
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
                if wg_obj and wg_col_name in wg_obj.columns:
                    wg_expr: Expr = make_column_expr(ctx.model, wg_obj_name, wg_col_name)
                else:
                    wg_expr = ColumnRef(name=wg_col_name, table=wg_obj_name)
                order_by = [
                    OrderByItem(expr=wg_expr, desc=wg.order.upper() == "DESC"),
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
        return metric_resolution.resolve_metric(self, ctx, name, metric)

    def _validate_partition_dimensions(
        self,
        ctx: _ResolutionContext,
        metric_name: str,
        partition_by: list[str],
        path_template: str,
    ) -> bool:
        return metric_resolution.validate_partition_dimensions(
            self, ctx, metric_name, partition_by, path_template
        )

    def _resolve_window_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a window metric (rank/lag/lead/ntile/first_value/last_value)."""
        return metric_resolution.resolve_window_metric(self, ctx, name, metric)

    def _resolve_derived_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a derived metric to its combined expression."""
        return metric_resolution.resolve_derived_metric(self, ctx, name, metric)

    def _resolve_cumulative_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a cumulative metric referencing an existing measure."""
        return metric_resolution.resolve_cumulative_metric(self, ctx, name, metric)

    def _resolve_pop_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a period-over-period metric."""
        return metric_resolution.resolve_pop_metric(self, ctx, name, metric)

    def _collect_having_measure_refs(self, query: QueryObject, model: SemanticModel) -> list[str]:
        """Collect measure/metric names referenced in any HAVING filter.

        Walks ``query.having`` recursively (each entry is a
        ``QueryFilter`` or a ``QueryFilterGroup``) and returns the
        ordered, de-duplicated list of ``field`` values that name a
        known measure or metric in the model. Order is preserved for
        deterministic resolution; duplicates are dropped on first sight.
        """

        seen: set[str] = set()
        out: list[str] = []
        measure_names = model.effective_measures

        def _visit(item: QueryFilterItem) -> None:
            if isinstance(item, QueryFilterGroup):
                for child in item.filters:
                    _visit(child)
                return
            field = item.field
            if field in seen:
                return
            if field in measure_names or field in model.metrics:
                seen.add(field)
                out.append(field)

        for entry in query.having:
            _visit(entry)
        return out

    def _get_measure_source_objects(self, ctx: _ResolutionContext, name: str) -> set[str]:
        """Extract all source data objects for a measure or metric."""
        result: set[str] = set()

        measure = ctx.model.effective_measures.get(name)
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
            elif metric.type == MetricType.WINDOW and metric.measure:
                # Window metric: source objects come from the referenced measure
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
        return filter_resolution.resolve_static_filter(self, ctx, mf)

    # -- filters -------------------------------------------------------------

    def _resolve_filter_object(
        self,
        ctx: _ResolutionContext,
        obj_name: str,
        filter_path: str,
        _field_label: str,
    ) -> bool:
        """Ensure *obj_name* is joined; auto-extend if reachable.

        Silently skips filters on unreachable data objects — they are
        irrelevant to the current query.
        """
        return filter_resolution.resolve_filter_object(
            self, ctx, obj_name, filter_path, _field_label
        )

    def _resolve_filter_item(
        self, ctx: _ResolutionContext, item: QueryFilterItem, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a filter item (leaf or group) to a physical expression."""
        return filter_resolution.resolve_filter_item(self, ctx, item, is_having=is_having)

    def _resolve_filter_group(
        self, ctx: _ResolutionContext, group: QueryFilterGroup, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a filter group recursively, combining with AND/OR."""
        return filter_resolution.resolve_filter_group(self, ctx, group, is_having=is_having)

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
        return filter_resolution.resolve_filter(self, ctx, qf, is_having=is_having)

    # -- order by ------------------------------------------------------------

    def _resolve_order_by_field(
        self, ctx: _ResolutionContext, field_name: str, select_count: int
    ) -> Expr | None:
        """Resolve an order-by field to its expression."""
        return filter_resolution.resolve_order_by_field(self, ctx, field_name, select_count)


class ResolutionError(Exception):
    """Raised when query resolution encounters errors."""

    def __init__(self, errors: list[SemanticError]) -> None:
        self.errors = errors
        messages = "; ".join(e.message for e in errors)
        super().__init__(f"Resolution errors: {messages}")
