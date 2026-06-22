"""Orchestrates the full compilation pipeline: Query → Resolution → Planning → AST → SQL."""

from __future__ import annotations

from dataclasses import dataclass, field

from orionbelt.compiler.cfl import CFLPlanner
from orionbelt.compiler.codegen import CodeGenerator
from orionbelt.compiler.fanout import detect_fanout
from orionbelt.compiler.passes import CompileContext, apply_aggregate_passes
from orionbelt.compiler.raw import RawPlanner
from orionbelt.compiler.resolution import QueryResolver, ResolvedQuery
from orionbelt.compiler.star import QueryPlan, StarSchemaPlanner
from orionbelt.compiler.validator import validate_sql
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryFilter, QueryFilterGroup, QueryFilterItem, QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.warnings import WarningCode, warning


@dataclass
class ResolvedInfo:
    """Summary of what was resolved during compilation."""

    fact_tables: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)


@dataclass
class ExplainJoin:
    """Explanation of a single join step in the query plan."""

    from_object: str
    to_object: str
    join_columns: list[str]
    reason: str
    cardinality: str = ""


@dataclass
class ExplainCflLeg:
    """Explanation of a single CFL leg."""

    measure_source: str
    common_root: str
    reason: str
    measures: list[str]
    joins: list[str]


@dataclass
class ExplainPlan:
    """Full explanation of the query planner decisions."""

    planner: str
    planner_reason: str
    base_object: str
    base_object_reason: str
    joins: list[ExplainJoin] = field(default_factory=list)
    where_filter_count: int = 0
    having_filter_count: int = 0
    has_totals: bool = False
    has_grain_overrides: bool = False
    has_filter_context: bool = False
    has_cumulative: bool = False
    has_pop: bool = False
    has_window: bool = False
    cfl_legs: list[ExplainCflLeg] = field(default_factory=list)


@dataclass
class CompilationResult:
    """The result of compiling a query to SQL."""

    sql: str
    dialect: str
    resolved: ResolvedInfo
    warnings: list[SemanticError] = field(default_factory=list)
    sql_valid: bool = True
    explain: ExplainPlan | None = None
    physical_tables: list[str] = field(default_factory=list)
    """Deduplicated list of ``DATABASE.SCHEMA.CODE`` triples reached by the
    query. Drives freshness-cache TTL composition and heartbeat
    invalidation. See ``design/PLAN_freshness_driven_cache.md`` §8."""


def _collect_subquery_objects(items: list[QueryFilterItem]) -> set[str]:
    """Recursively gather every ``Subquery.dataObject`` referenced by EXISTS /
    NONEXISTS filters in a list of where/having items.

    Required by :func:`_compute_physical_tables` so the cache key reflects
    every table the compiled SQL actually reads — without this, an
    ``EXISTS OrderItems`` filter looks like a single-fact query for
    ``Orders``, and child-table updates can't invalidate the cached result.
    """
    found: set[str] = set()
    for item in items:
        if isinstance(item, QueryFilterGroup):
            found.update(_collect_subquery_objects(list(item.filters)))
        elif isinstance(item, QueryFilter) and item.subquery is not None:
            found.add(item.subquery.data_object)
            # Sub-filters can themselves carry EXISTS; walk them too.
            found.update(_collect_subquery_objects(list(item.subquery.filter)))
    return found


def _compute_physical_tables(
    resolved: ResolvedQuery, query: QueryObject, model: SemanticModel
) -> list[str]:
    """Deduplicate dataObjects to physical ``DATABASE.SCHEMA.CODE`` triples.

    Two dataObjects mapping to the same physical table contribute one entry.
    See ``design/PLAN_freshness_driven_cache.md`` §10 for why this matters
    (one source, multiple semantic facets like ``Sales`` + ``Returns``).

    EXISTS / NONEXISTS target dataObjects are not part of
    ``resolved.required_objects`` (they live behind a correlated subquery,
    not in the FROM/JOIN chain), so the original query is walked too —
    otherwise child-table edits would not invalidate cached results.
    """
    object_names: set[str] = set(resolved.required_objects)
    object_names.update(_collect_subquery_objects(list(query.where)))
    object_names.update(_collect_subquery_objects(list(query.having)))

    seen: set[str] = set()
    out: list[str] = []
    for name in sorted(object_names):
        obj = model.data_objects.get(name)
        if obj is None:
            continue
        parts = [str(p) for p in (obj.database, obj.schema_name, obj.code) if p]
        if not parts:
            continue
        ref = ".".join(parts)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


class CompilationPipeline:
    """Orchestrates: Query → Resolution → Planning → AST → SQL."""

    def __init__(self) -> None:
        self._resolver = QueryResolver()
        self._star_planner = StarSchemaPlanner()
        self._cfl_planner = CFLPlanner()
        self._raw_planner = RawPlanner()

    def compile(
        self,
        query: QueryObject,
        model: SemanticModel,
        dialect_name: str,
    ) -> CompilationResult:
        """Compile a query to SQL for the specified dialect."""
        # Create dialect first so resolution and planning share one
        # ``qualify_table`` — the EXISTS filter operator needs it during
        # resolution to render the correlated subquery's FROM clause.
        dialect = DialectRegistry.get(dialect_name)
        qualify_table = lambda obj: dialect.format_table_ref(  # noqa: E731
            obj.database, obj.schema_name, obj.code
        )

        # Phase 1: Resolution
        resolved = self._resolver.resolve(query, model, qualify_table=qualify_table)

        # Phase 1.5: Fanout detection (skip for CFL — each fact queried independently)
        if not resolved.requires_cfl:
            detect_fanout(resolved, model)

        # Phase 2: Planning (raw / star schema / CFL)
        use_cfl = resolved.requires_cfl or resolved.dimensions_exclude
        if resolved.is_raw:
            plan = self._raw_planner.plan(
                resolved,
                model,
                qualify_table=qualify_table,
                dialect=dialect,
                union_by_name=dialect.capabilities.supports_union_all_by_name,
            )
        elif use_cfl:
            plan = self._cfl_planner.plan(
                resolved,
                model,
                qualify_table=qualify_table,
                union_by_name=dialect.capabilities.supports_union_all_by_name,
                dialect=dialect,
            )
        else:
            plan = self._star_planner.plan(
                resolved, model, qualify_table=qualify_table, dialect=dialect
            )

        # Phase 2.3 – 2.6: Aggregate-mode passes (filter context, PoP,
        # totals, cumulative, window) plus HAVING projection cleanup. Raw
        # mode has no measures, so the passes are no-ops and skipped
        # entirely for clarity. Pass ordering and the feature-compatibility
        # rules live in ``compiler/passes.py``.
        if resolved.is_raw:
            wrapped_ast = plan.ast
        else:
            ctx = CompileContext(
                resolved=resolved,
                model=model,
                dialect=dialect,
                qualify_table=qualify_table,
            )
            wrapped_ast = apply_aggregate_passes(plan.ast, ctx)

        # Phase 3: Dialect-specific SQL rendering
        codegen = CodeGenerator(dialect)
        sql = codegen.generate(wrapped_ast)

        # Phase 4: SQL validation (non-blocking)
        validation_errors = validate_sql(sql, dialect_name)
        sql_valid = len(validation_errors) == 0
        warnings = resolved.warnings
        if not sql_valid:
            warnings = warnings + [
                warning(
                    code=WarningCode.SQL_VALIDATION,
                    message=f"SQL validation: {e}",
                )
                for e in validation_errors
            ]

        # Build explain plan
        explain = self._build_explain(resolved, model, use_cfl, plan)

        # Compute deduplicated physical tables touched by the query
        physical_tables = _compute_physical_tables(resolved, query, model)

        return CompilationResult(
            sql=sql,
            dialect=dialect_name,
            physical_tables=physical_tables,
            resolved=ResolvedInfo(
                fact_tables=resolved.fact_tables,
                dimensions=[d.name for d in resolved.dimensions],
                measures=[m.name for m in resolved.measures],
            ),
            warnings=warnings,
            sql_valid=sql_valid,
            explain=explain,
        )

    @staticmethod
    def _q(name: str) -> str:
        """Quote an identifier for explain output."""
        return f'"{name}"'

    def _build_explain(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        use_cfl: bool,
        plan: QueryPlan,
    ) -> ExplainPlan:
        """Build the explain plan from resolution results."""
        q = self._q

        # Planner choice
        if resolved.is_raw:
            planner = "Raw"
            distinct_note = " with DISTINCT" if resolved.distinct else ""
            planner_reason = (
                f"Raw-mode projection of physical columns{distinct_note} — "
                f"no aggregation, no GROUP BY"
            )
        elif use_cfl:
            if resolved.dimensions_exclude:
                planner = "CFL"
                planner_reason = (
                    "dimensionsExclude anti-join — "
                    "CROSS JOIN of distinct values EXCEPT existing combinations"
                )
            else:
                planner = "CFL"
                sources = ", ".join(q(s) for s in sorted(resolved.measure_source_objects))
                planner_reason = (
                    f"Measures reference independent fact tables ({sources}) — "
                    f"Composite Fact Layer merges them via UNION ALL"
                )
        else:
            planner = "Star Schema"
            planner_reason = (
                "All requested objects are reachable from a single base via directed joins"
            )

        # Base object — explain should reflect actual selection logic
        base = resolved.base_object
        if resolved.measure_source_objects:
            if use_cfl and len(resolved.measure_source_objects) > 1:
                base_reason = (
                    "Not applicable — each CFL leg uses its own common root (see cfl_legs)"
                )
            elif len(resolved.measure_source_objects) > 1:
                sources = ", ".join(q(s) for s in sorted(resolved.measure_source_objects))
                base_reason = (
                    f"{q(base)} selected as base — most connected fact table "
                    f"among measure sources ({sources})"
                )
            else:
                base_reason = f"{q(base)} selected as base — sole measure source object"
        elif len(resolved.required_objects) > 1:
            base_reason = (
                f"{q(base)} selected as base — common root that can reach "
                f"all required objects via directed joins"
            )
        else:
            base_reason = f"{q(base)} selected as base for single-object query"

        # Joins — for CFL queries the per-leg joins are more informative,
        # so only include resolution-level joins for star schema queries.
        explain_joins: list[ExplainJoin] = []
        if not use_cfl:
            for step in resolved.join_steps:
                join_cols = [
                    f"{fc} = {tc}"
                    for fc, tc in zip(step.from_columns, step.to_columns, strict=True)
                ]
                if step.reversed:
                    reason = (
                        f"Reversed join from {q(step.from_object)} to {q(step.to_object)} — "
                        f"original join was defined in the opposite direction"
                    )
                else:
                    reason = (
                        f"Join {q(step.from_object)} → {q(step.to_object)} to include "
                        f"columns needed by the query"
                    )
                explain_joins.append(
                    ExplainJoin(
                        from_object=step.from_object,
                        to_object=step.to_object,
                        join_columns=join_cols,
                        reason=reason,
                        cardinality=step.cardinality.value,
                    )
                )

        # CFL leg details
        cfl_leg_explains: list[ExplainCflLeg] = []
        for leg in plan.cfl_legs:
            cfl_leg_explains.append(
                ExplainCflLeg(
                    measure_source=leg.measure_source,
                    common_root=leg.common_root,
                    reason=leg.reason,
                    measures=leg.measures,
                    joins=leg.joins,
                )
            )

        return ExplainPlan(
            planner=planner,
            planner_reason=planner_reason,
            base_object=base,
            base_object_reason=base_reason,
            joins=explain_joins,
            where_filter_count=len(resolved.where_filters),
            having_filter_count=len(resolved.having_filters),
            has_totals=resolved.has_totals,
            has_grain_overrides=resolved.has_grain_overrides,
            has_filter_context=resolved.has_filter_context,
            has_cumulative=resolved.has_cumulative,
            has_pop=resolved.has_pop,
            has_window=resolved.has_window,
            cfl_legs=cfl_leg_explains,
        )
