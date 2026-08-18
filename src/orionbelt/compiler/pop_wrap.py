"""Wrapper CTEs for period-over-period (PoP) metrics.

Generates four CTEs using the synthetical date pattern:

| CTE           | Purpose                                                |
|---------------|--------------------------------------------------------|
| date_range    | Discover MIN/MAX date from fact tables (with filters)  |
| date_spine    | Generate series with spine_date / spine_date_prev      |
| pop_base      | Aggregate measures using spine as FROM, facts LEFT JOIN |
| pop_compare   | Self-join pop_base via spine_date_prev for comparison   |

The wrapper follows the same CTE pattern as ``total_wrap.py`` and
``cumulative_wrap.py``: the planner output is restructured into a
date-spine-driven query, and the comparison layer is added on top.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    OrderByItem,
    RawSQL,
    Select,
)
from orionbelt.compiler.expr_rewrite import map_column_refs
from orionbelt.compiler.having_hoist import windowed_aliases
from orionbelt.compiler.metric_expansion import expand_metric_expression
from orionbelt.compiler.resolution import ResolutionError, ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.type_resolver import (
    apply_exact_integer_avg,
    cast_measure_to_resolved_type,
    resolve_metric_data_type,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import PeriodOverPeriodComparison, SemanticModel

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import DataObject


def _apply_metric_cast(
    expr: Expr,
    metric_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Wrap a PoP projection with the metric's declared dataType cast.

    Same shape as ``cumulative_wrap._apply_metric_cast`` and
    ``window_wrap._apply_metric_cast``. PoP was the only metric wrapper that
    never applied the declared type, so its output scale was whatever each
    engine's decimal division produced.

    The cast governs the *result*; it cannot recover precision the engine
    already discarded mid-division. That is what
    ``Dialect.render_decimal_division_sql`` is for, and why ClickHouse still
    widens its operands before dividing - see the note there.
    """
    if model is None or dialect is None:
        return expr
    metric = model.metrics.get(metric_name)
    if metric is None:
        return expr
    resolved_type = resolve_metric_data_type(metric, model.settings)
    if resolved_type is None:
        return expr
    return dialect.cast_to_obml_type(expr, resolved_type)


def _apply_measure_cast(
    expr: Expr,
    measure_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Wrap a base aggregate with the measure's declared dataType cast.

    ``pop_base`` rebuilds the aggregation itself rather than reusing the
    planner's projection, so without this it emitted a bare ``SUM(...)`` where
    every other plan emits ``CAST(SUM(...) AS DECIMAL(18, 2))``. A PoP query
    therefore returned a *different type* for the same measure than the same
    query without a PoP metric, and the ``difference`` / ``previousValue``
    comparisons built on top had no declared type to inherit.
    """
    if model is None or dialect is None:
        return expr
    measure = model.effective_measures.get(measure_name)
    if measure is None:
        return expr
    return cast_measure_to_resolved_type(expr, measure, model.settings, dialect)


def _apply_base_measure_rewrite(
    expr: Expr,
    metric_name: str,
    model: SemanticModel | None,
    dialect: Dialect | None,
) -> Expr:
    """Make a wrapper metric's base aggregate exact, without casting it.

    A window or cumulative metric's placeholder column holds the *base
    measure's* aggregate, so the exactness rule is the base measure's too. The
    cast is deliberately withheld here - it would truncate the window's input -
    but the rewrite is not, and withholding both is what left an integer AVG
    reaching the window as a raw floating average on the rewrite-only dialects.
    """
    if model is None or dialect is None:
        return expr
    metric = model.metrics.get(metric_name)
    base_name = getattr(metric, "measure", None) if metric else None
    if not base_name:
        return expr
    base = model.effective_measures.get(base_name)
    if base is None:
        return expr
    return apply_exact_integer_avg(expr, base, model.settings, dialect)


def _resolve_col_code(model: SemanticModel, obj_name: str, display_name: str) -> str:
    """Resolve a column display name to its physical code."""
    obj = model.data_objects.get(obj_name)
    if obj and display_name in obj.columns:
        return obj.columns[display_name].code
    return display_name


def wrap_with_pop(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
) -> Select:
    """Wrap a planner AST with PoP CTEs if any period-over-period metrics are present.

    If no PoP metrics are present, returns ``ast`` unchanged.
    """
    if not resolved.has_pop:
        return ast

    pop_measures = [m for m in resolved.measures if m.is_pop]

    # PoP metrics in one query may use different comparison offsets (e.g. MoM
    # + YoY), but they share a single date spine, so they must agree on the
    # time dimension and the base grain (the spine's bucket size). Offsets are
    # handled per-metric in ``_build_pop_compare_sql``.
    pop_config = pop_measures[0]
    for other in pop_measures[1:]:
        if (
            other.pop_time_dimension != pop_config.pop_time_dimension
            or other.pop_grain != pop_config.pop_grain
        ):
            raise ResolutionError(
                [
                    SemanticError(
                        code="INVALID_METRIC",
                        message=(
                            "Cannot combine period-over-period metrics computed at "
                            f"different time grains: '{pop_config.name}' compares over "
                            f"{pop_config.pop_time_dimension} ({pop_config.pop_grain}), "
                            f"but '{other.name}' compares over "
                            f"{other.pop_time_dimension} ({other.pop_grain}). Metrics "
                            "that compare across time must share one time dimension and "
                            "grain (only the comparison offset may differ). Keep metrics "
                            "of one grain per query, or query each separately."
                        ),
                        path="metrics",
                        hint=(
                            "Remove one of the conflicting metrics, or run a separate "
                            "query per time grain."
                        ),
                        context={
                            "metricA": pop_config.name,
                            "timeDimensionA": pop_config.pop_time_dimension,
                            "grainA": pop_config.pop_grain,
                            "metricB": other.name,
                            "timeDimensionB": other.pop_time_dimension,
                            "grainB": other.pop_grain,
                        },
                    )
                ]
            )
    if pop_config.pop_time_dimension is None:
        raise ResolutionError(
            [
                SemanticError(
                    code="INVALID_METRIC",
                    message="PoP metric missing required timeDimension",
                    path="metrics",
                )
            ]
        )
    if pop_config.pop_grain is None:
        raise ResolutionError(
            [
                SemanticError(
                    code="INVALID_METRIC",
                    message="PoP metric missing required grain",
                    path="metrics",
                )
            ]
        )
    if pop_config.pop_offset_grain is None:
        raise ResolutionError(
            [
                SemanticError(
                    code="INVALID_METRIC",
                    message="PoP metric missing required offsetGrain",
                    path="metrics",
                )
            ]
        )

    grain = pop_config.pop_grain.value
    offset = pop_config.pop_offset
    offset_grain = pop_config.pop_offset_grain.value
    time_dim_name = pop_config.pop_time_dimension

    # Resolve the time dimension's physical column and object
    time_dim = next(d for d in resolved.dimensions if d.name == time_dim_name)
    time_obj_name = time_dim.object_name
    time_source_col = time_dim.source_column

    # --- CTE 1: date_range ---
    date_range_sql = _build_date_range_sql(
        resolved, model, dialect, qualify_table, grain, time_dim_name
    )
    # RawSQL: dialect-specific date aggregation/casts the SQL AST does not model.
    # Covered by the PoP drift snapshots. See tests/architecture/test_rawsql_guard.py.
    date_range_cte = CTE(name="date_range", query=RawSQL(sql=date_range_sql))

    # --- CTE 2: date_spine ---
    # Use scalar subqueries so every dialect can resolve date_range references
    # without needing date_range in their FROM clause (universally compatible).
    # Quote the CTE name so it matches the quoted declaration on case-folding
    # dialects (Snowflake folds a bare ``date_range`` to ``DATE_RANGE``).
    date_range_ref = dialect.quote_identifier("date_range")
    spine_sql = dialect.render_date_spine_cte_sql(
        min_date=f"(SELECT min_date FROM {date_range_ref})",
        max_date=f"(SELECT max_date FROM {date_range_ref})",
        grain=grain,
        offset=offset,
        offset_grain=offset_grain,
    )
    # RawSQL: per-dialect date-spine generator (recursive CTE / generate_series /
    # sequence) — not expressible as a typed Select. Covered by PoP drift snapshots.
    date_spine_cte = CTE(name="date_spine", query=RawSQL(sql=spine_sql))

    # --- CTE 3: pop_base ---
    # Build FROM date_spine with LEFT JOINs to fact and dimension tables
    # Re-use the planner's join structure but restructured
    pop_base_sql = _build_pop_base_sql(
        ast, resolved, model, dialect, qualify_table, grain, time_obj_name, time_source_col
    )
    # RawSQL: restructured join tree anchored on the date spine with dialect date
    # arithmetic; not the planner's Select shape. Covered by PoP drift snapshots.
    pop_base_cte = CTE(name="pop_base", query=RawSQL(sql=pop_base_sql))

    # --- CTE 4: pop_compare ---
    pop_compare_sql = _build_pop_compare_sql(resolved, dialect, pop_measures)
    # RawSQL: dynamic self-joins (one per distinct PoP offset) with inline date
    # arithmetic; not a fixed typed Select. Covered by PoP drift snapshots.
    pop_compare_cte = CTE(name="pop_compare", query=RawSQL(sql=pop_compare_sql))

    # --- Final SELECT from pop_compare ---
    outer_columns: list[Expr] = []
    for dim in resolved.dimensions:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
    for m in resolved.measures:
        column: Expr = ColumnRef(name=m.name)
        if m.is_pop:
            # The comparison is assembled as raw SQL inside ``pop_compare``, so
            # the metric's declared dataType has to be applied here, over the
            # materialised column. Without it the ratio carries whatever scale
            # the engine's decimal division happens to produce, which differs
            # per engine: for a metric declared ``decimal(18, 4)`` DuckDB
            # returned 0.9931620307032472, BigQuery 0.993162031 and Snowflake
            # 0.99316203, none of them the declared 0.9932. ``cumulative_wrap``
            # and ``window_wrap`` already cast their own projections this way.
            column = _apply_metric_cast(column, m.name, model, dialect)
        outer_columns.append(AliasedExpr(expr=column, alias=m.name))

    # Remap ORDER BY to alias-only refs (dimension/measure names, not physical codes)
    outer_order_by = _build_outer_order_by(resolved)

    # Apply HAVING filters here. In a PoP query the measures and PoP metrics are
    # materialised columns in ``pop_compare``, so a HAVING predicate on them
    # becomes a plain WHERE over that CTE (the metric is already computed, so the
    # filter references it by alias). The star planner applies these at GROUP BY
    # level, which the PoP rewrite bypasses entirely — without this they were
    # silently dropped, returning unfiltered rows.
    # Two independent rules apply to the predicates this wrapper emits, and
    # they do not overlap: ``windowed_aliases`` never contains a PoP metric.
    #
    # 1. A predicate on a *windowed* alias belongs to ``PASS_HAVING_WINDOW``,
    #    which applies it after the wrapper nesting this one has run.
    # 2. A PoP metric's declared dataType governs its value, so the filter has
    #    to see the same value the projection returns. Both live in this one
    #    SELECT, and WHERE is evaluated before the select list, so a bare alias
    #    reference would read ``pop_compare``'s *uncast* column: with a metric
    #    declared decimal(18, 4), `> 0.99318` dropped a row whose returned
    #    value is 0.9932, because the underlying ratio is 0.9931620307.
    deferred = windowed_aliases(resolved)
    pop_metrics = {m.name for m in resolved.measures if m.is_pop}

    def _typed(ref: ColumnRef) -> Expr:
        if ref.table is None and ref.name in pop_metrics:
            return _apply_metric_cast(ref, ref.name, model, dialect)
        return ref

    outer_where: Expr | None = None
    for hf in resolved.having_filters:
        if hf.referenced_fields & deferred:
            continue
        predicate = map_column_refs(hf.expression, _typed)
        outer_where = (
            predicate
            if outer_where is None
            else BinaryOp(left=outer_where, op="AND", right=predicate)
        )

    # Collect all CTEs (planner CTEs + our 4 new ones)
    all_ctes = list(ast.ctes) + [date_range_cte, date_spine_cte, pop_base_cte, pop_compare_cte]

    return Select(
        columns=outer_columns,
        from_=From(source="pop_compare", alias="pop_compare"),
        joins=[],
        where=outer_where,
        group_by=[],
        having=None,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _build_date_range_sql(
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    grain: str,
    time_dim_name: str,
) -> str:
    """Build the raw SQL body for the date_range CTE.

    Scans fact tables for MIN/MAX of the time dimension column,
    with ALL query WHERE filters pushed down.
    """
    # Collect all fact tables that need scanning
    fact_objects = (
        sorted(resolved.measure_source_objects) if resolved.measure_source_objects else []
    )
    if not fact_objects and resolved.base_object:
        fact_objects = [resolved.base_object]

    # Resolve the time dimension's physical info per object
    time_dim = next(d for d in resolved.dimensions if d.name == time_dim_name)
    time_obj_name = time_dim.object_name
    time_source_col = time_dim.source_column

    # Build WHERE clause from resolved filters
    where_parts: list[str] = []
    for rf in resolved.where_filters:
        where_sql = dialect.compile_expr(rf.expression)
        where_parts.append(where_sql)
    where_clause = ""
    if where_parts:
        where_clause = "\n  WHERE " + " AND ".join(where_parts)

    # Build join clauses for filter push-down (same joins as the main query)
    join_clauses: list[str] = []
    for step in resolved.join_steps:
        to_obj = model.data_objects.get(step.to_object)
        if to_obj is None:
            continue
        to_table = qualify_table(to_obj)
        to_alias = dialect.quote_identifier(step.to_object)
        on_parts = []
        for fc, tc in zip(step.from_columns, step.to_columns, strict=True):
            from_q = dialect.quote_identifier(step.from_object)
            fc_code = _resolve_col_code(model, step.from_object, fc)
            tc_code = _resolve_col_code(model, step.to_object, tc)
            from_ref = f"{from_q}.{dialect.quote_identifier(fc_code)}"
            to_ref = f"{to_alias}.{dialect.quote_identifier(tc_code)}"
            on_parts.append(f"{from_ref} = {to_ref}")
        on_clause = " AND ".join(on_parts)
        join_clauses.append(f"\n  LEFT JOIN {to_table} AS {to_alias} ON {on_clause}")

    joins_sql = "".join(join_clauses)

    if len(fact_objects) <= 1:
        # Single fact: direct scan
        obj_name = fact_objects[0] if fact_objects else time_obj_name
        obj = model.data_objects[obj_name]
        table_ref = qualify_table(obj)
        alias = dialect.quote_identifier(obj_name)
        time_alias = dialect.quote_identifier(time_obj_name)
        time_col = f"{time_alias}.{dialect.quote_identifier(time_source_col)}"
        trunc_min = dialect.render_date_trunc_sql(f"MIN({time_col})", grain)
        trunc_max = dialect.render_date_trunc_sql(f"MAX({time_col})", grain)

        return (
            f"SELECT {trunc_min} AS min_date,\n"
            f"       {trunc_max} AS max_date\n"
            f"  FROM {table_ref} AS {alias}{joins_sql}{where_clause}"
        )

    # Multi-fact (CFL): UNION ALL across fact tables
    legs: list[str] = []
    for obj_name in fact_objects:
        obj = model.data_objects[obj_name]
        table_ref = qualify_table(obj)
        alias = dialect.quote_identifier(obj_name)

        # Use the time dimension's table alias (may differ from the fact table)
        time_alias = dialect.quote_identifier(time_obj_name)
        time_col = f"{time_alias}.{dialect.quote_identifier(time_source_col)}"
        trunc_min = dialect.render_date_trunc_sql(f"MIN({time_col})", grain)
        trunc_max = dialect.render_date_trunc_sql(f"MAX({time_col})", grain)

        legs.append(
            f"SELECT {trunc_min} AS min_date,\n"
            f"           {trunc_max} AS max_date\n"
            f"      FROM {table_ref} AS {alias}{joins_sql}{where_clause}"
        )

    inner = "\n    UNION ALL\n    ".join(legs)
    return (
        f"SELECT MIN(min_date) AS min_date, MAX(max_date) AS max_date\n"
        f"  FROM (\n    {inner}\n  ) AS ranges"
    )


def _build_pop_base_sql(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    grain: str,
    time_obj_name: str,
    time_source_col: str,
) -> str:
    """Build the raw SQL body for the pop_base CTE.

    FROM date_spine, LEFT JOIN fact tables and dimension tables,
    GROUP BY spine_date + non-time dimensions.
    """
    # Quote the spine CTE name so references match the quoted declaration on
    # case-folding dialects (Snowflake); ``spine_date`` etc. stay bare, matching
    # the spine's own bare column aliases.
    spine_cte = dialect.quote_identifier("date_spine")

    # Dimension aliases: d1 = time dim (spine_date), d2..dN = others
    dim_selects: list[str] = []
    dim_groups: list[str] = []

    for d_idx, dim in enumerate(resolved.dimensions, 1):
        pop_measure = next((m for m in resolved.measures if m.is_pop), None)
        if pop_measure and dim.name == pop_measure.pop_time_dimension:
            dim_selects.append(f"{spine_cte}.spine_date AS {dialect.quote_identifier(dim.name)}")
        else:
            obj_alias = dialect.quote_identifier(dim.object_name)
            col = dialect.quote_identifier(dim.source_column)
            dim_selects.append(f"{obj_alias}.{col} AS {dialect.quote_identifier(dim.name)}")
        dim_groups.append(str(d_idx))

    # Measure selects
    measure_selects: list[str] = []
    for m in resolved.measures:
        if m.is_pop:
            # For PoP metrics, we need the base measure(s)
            for comp_name in m.component_measures:
                comp = resolved.metric_components.get(comp_name)
                if comp:
                    comp_expr = _apply_measure_cast(comp.expression, comp_name, model, dialect)
                    expr_sql = dialect.compile_expr(comp_expr)
                    measure_selects.append(f"{expr_sql} AS {dialect.quote_identifier(comp_name)}")
        else:
            # A metric rides along as its components' aggregates, not as its
            # formula: the formula's placeholders are the *aliases* of columns
            # this same SELECT list is producing, which only the dialects with
            # lateral alias references would resolve — and a nested derived
            # metric names a column nothing projects at all.
            expr = (
                expand_metric_expression(
                    m.expression, resolved.metric_components, lambda comp: comp.expression
                )
                if m.component_measures
                else m.expression
            )
            # Same cast the star planner applies, so a measure keeps its
            # declared type whether or not the query also has a PoP metric.
            #
            # A *wrapper* metric is excluded. Its column here is only a
            # placeholder holding the base measure's aggregate until
            # ``window_wrap`` / ``cumulative_wrap`` builds the real window
            # call, so the metric's own dataType does not describe it yet.
            # Casting it early corrupts the input: a rank declaring
            # ``dataType: integer`` truncated ``SUM(amount)`` to INT, so 1.49
            # and 1.40 both became 1 and ranked equal. Those wrappers apply
            # the metric's type to the finished window value themselves.
            #
            # The *rewrite* is a separate matter from the cast and does apply
            # here: an integer AVG has to reach the window already exact, or
            # the wrapper carries a raw floating average that no later cast can
            # repair. Skipping both left the rewrite-only dialects drifting
            # exactly where this machinery exists to stop them.
            is_wrapper_metric = m.is_window or m.is_cumulative
            if m.component_measures and not is_wrapper_metric:
                expr = _apply_metric_cast(expr, m.name, model, dialect)
            elif not m.component_measures:
                expr = _apply_measure_cast(expr, m.name, model, dialect)
            elif is_wrapper_metric:
                expr = _apply_base_measure_rewrite(expr, m.name, model, dialect)
            expr_sql = dialect.compile_expr(expr)
            measure_selects.append(f"{expr_sql} AS {dialect.quote_identifier(m.name)}")

    # Deduplicate measure selects (PoP metrics may share components)
    seen_measures: set[str] = set()
    unique_measure_selects: list[str] = []
    for ms in measure_selects:
        # Extract alias from "... AS alias"
        parts = ms.rsplit(" AS ", 1)
        alias = parts[-1] if len(parts) == 2 else ms
        if alias not in seen_measures:
            seen_measures.add(alias)
            unique_measure_selects.append(ms)

    all_selects = dim_selects + unique_measure_selects
    select_clause = ",\n       ".join(all_selects)

    # FROM date_spine
    from_clause = spine_cte
    base_obj_name = resolved.base_object or time_obj_name

    # ── Build LEFT JOINs ──
    # Case 1: time column is on the base fact table (common case)
    #   → LEFT JOIN fact ON date_trunc(fact.time_col) = spine_date
    # Case 2: time column is on a different (dimension) table
    #   → LEFT JOIN time_table ON date_trunc(time_table.time_col) = spine_date
    #   → LEFT JOIN fact ON fact.fk = time_table.pk  (reversed join step)
    joined_objects: set[str] = set()
    join_clauses: list[str] = []

    # Step A: LEFT JOIN the time dimension's table onto the spine
    time_obj = model.data_objects[time_obj_name]
    time_table = qualify_table(time_obj)
    time_alias_q = dialect.quote_identifier(time_obj_name)
    time_col = f"{time_alias_q}.{dialect.quote_identifier(time_source_col)}"
    trunc_col = dialect.render_date_trunc_sql(time_col, grain)
    join_clauses.append(
        f"\n  LEFT JOIN {time_table} AS {time_alias_q}\n    ON {trunc_col} = {spine_cte}.spine_date"
    )
    joined_objects.add(time_obj_name)

    # Step B: If base fact is different from time table, find the join step to connect them
    if base_obj_name != time_obj_name and base_obj_name not in joined_objects:
        for step in resolved.join_steps:
            if step.from_object == base_obj_name and step.to_object == time_obj_name:
                # Reverse: JOIN base_fact ON base.fk = time_table.pk
                base_obj = model.data_objects[base_obj_name]
                base_table = qualify_table(base_obj)
                base_alias_q = dialect.quote_identifier(base_obj_name)
                on_parts = []
                for fc, tc in zip(step.from_columns, step.to_columns, strict=True):
                    fc_code = _resolve_col_code(model, step.from_object, fc)
                    tc_code = _resolve_col_code(model, step.to_object, tc)
                    on_parts.append(
                        f"{base_alias_q}.{dialect.quote_identifier(fc_code)}"
                        f" = {time_alias_q}.{dialect.quote_identifier(tc_code)}"
                    )
                join_clauses.append(
                    f"\n  LEFT JOIN {base_table} AS {base_alias_q} ON {' AND '.join(on_parts)}"
                )
                joined_objects.add(base_obj_name)
                break

    # Step C: Add remaining dimension table joins (from resolved join_steps)
    for step in resolved.join_steps:
        if step.to_object in joined_objects:
            continue
        to_obj = model.data_objects.get(step.to_object)
        if to_obj is None:
            continue
        to_table = qualify_table(to_obj)
        to_alias = dialect.quote_identifier(step.to_object)
        on_parts = []
        for fc, tc in zip(step.from_columns, step.to_columns, strict=True):
            from_q = dialect.quote_identifier(step.from_object)
            fc_code = _resolve_col_code(model, step.from_object, fc)
            tc_code = _resolve_col_code(model, step.to_object, tc)
            from_ref = f"{from_q}.{dialect.quote_identifier(fc_code)}"
            to_ref = f"{to_alias}.{dialect.quote_identifier(tc_code)}"
            on_parts.append(f"{from_ref} = {to_ref}")
        on_clause = " AND ".join(on_parts)
        join_clauses.append(f"\n  LEFT JOIN {to_table} AS {to_alias} ON {on_clause}")
        joined_objects.add(step.to_object)

    joins_sql = "".join(join_clauses)
    group_clause = ", ".join(dim_groups)

    return f"SELECT {select_clause}\n  FROM {from_clause}{joins_sql}\n  GROUP BY {group_clause}"


def _build_pop_compare_sql(
    resolved: ResolvedQuery,
    dialect: Dialect,
    pop_measures: list[ResolvedMeasure],
) -> str:
    """Build the raw SQL body for the pop_compare CTE.

    Self-joins ``pop_base`` to compare each period against a prior one. PoP
    metrics may use *different* comparison offsets (e.g. month-over-month and
    year-over-year in the same query): the first measure's offset is served by
    the spine's precomputed ``spine_date_prev`` (so the common single-offset
    SQL is unchanged), and each additional distinct offset gets its own
    self-join whose prior date is computed inline with ``date_add_sql``.
    """
    pop_time_dim = pop_measures[0].pop_time_dimension
    time_q = dialect.quote_identifier(pop_time_dim or "")
    non_time_dims = [d for d in resolved.dimensions if d.name != pop_time_dim]

    # Quote the CTE names so references match the quoted declarations on
    # case-folding dialects (Snowflake). The self-join aliases (``pop_prev``)
    # stay bare — they are declared and referenced bare, so they already agree.
    base_cte = dialect.quote_identifier("pop_base")
    spine_cte = dialect.quote_identifier("date_spine")

    def _dim_match(alias: str) -> str:
        parts = [
            f"{base_cte}.{dialect.quote_identifier(d.name)} = "
            f"{alias}.{dialect.quote_identifier(d.name)}"
            for d in non_time_dims
        ]
        return (" AND " + " AND ".join(parts)) if parts else ""

    # Assign one self-join alias per distinct (offset, offset_grain). The
    # offset matching the spine (the first PoP measure's) reuses ``pop_prev``
    # via ``spine_date_prev``; others use ``pop_prev_N`` with an inline offset.
    spine_key = (pop_measures[0].pop_offset, pop_measures[0].pop_offset_grain)
    alias_by_key: dict[tuple[int | None, object], str] = {}
    join_clauses: list[str] = []
    for m in pop_measures:
        key = (m.pop_offset, m.pop_offset_grain)
        if key in alias_by_key:
            continue
        if key == spine_key and "pop_prev" not in alias_by_key.values():
            alias = "pop_prev"
            join_clauses.append(
                f"  LEFT JOIN {spine_cte} ON {base_cte}.{time_q} = {spine_cte}.spine_date"
            )
            # NB: alias is ``pop_prev`` (not ``prev``) — ``prev`` is a reserved
            # word in Dremio and rejects as an unquoted table alias.
            join_clauses.append(
                f"  LEFT JOIN {base_cte} AS {alias}\n"
                f"    ON {spine_cte}.spine_date_prev = {alias}.{time_q}{_dim_match(alias)}"
            )
        else:
            alias = f"pop_prev_{len(alias_by_key)}"
            grain_val = m.pop_offset_grain.value if m.pop_offset_grain else "month"
            prev_date = dialect.date_add_sql(f"{base_cte}.{time_q}", grain_val, m.pop_offset or 0)
            join_clauses.append(
                f"  LEFT JOIN {base_cte} AS {alias}\n"
                f"    ON {alias}.{time_q} = {prev_date}{_dim_match(alias)}"
            )
        alias_by_key[key] = alias

    # Projections: dimensions, pass-through measures, then PoP comparisons.
    selects: list[str] = []
    for dim in resolved.dimensions:
        q = dialect.quote_identifier(dim.name)
        selects.append(f"{base_cte}.{q} AS {q}")
    for m in resolved.measures:
        if not m.is_pop:
            q = dialect.quote_identifier(m.name)
            selects.append(f"{base_cte}.{q} AS {q}")
    for m in pop_measures:
        if m.pop_comparison is None:
            raise ResolutionError(
                [
                    SemanticError(
                        code="INVALID_METRIC",
                        message=f"PoP measure '{m.name}' missing comparison type",
                        path="metrics",
                    )
                ]
            )
        base_name = m.pop_base_measure or m.component_measures[0]
        q_base = dialect.quote_identifier(base_name)
        q_metric = dialect.quote_identifier(m.name)
        alias = alias_by_key[(m.pop_offset, m.pop_offset_grain)]
        current = f"{base_cte}.{q_base}"
        prev = f"{alias}.{q_base}"

        if m.pop_comparison == PeriodOverPeriodComparison.RATIO:
            expr = dialect.render_decimal_division_sql(current, prev)
        elif m.pop_comparison == PeriodOverPeriodComparison.DIFFERENCE:
            expr = f"{current} - {prev}"
        elif m.pop_comparison == PeriodOverPeriodComparison.PREVIOUS_VALUE:
            expr = dialect.render_pop_previous_value_sql(prev, current)
        elif m.pop_comparison == PeriodOverPeriodComparison.PERCENT_CHANGE:
            expr = dialect.render_decimal_division_sql(current, prev) + " - 1"
        else:
            raise ResolutionError(
                [
                    SemanticError(
                        code="INVALID_METRIC",
                        message=f"Unknown PoP comparison type: {m.pop_comparison}",
                        path="metrics",
                    )
                ]
            )
        selects.append(f"{expr} AS {q_metric}")

    select_clause = ",\n       ".join(selects)
    return f"SELECT {select_clause}\n  FROM {base_cte}\n" + "\n".join(join_clauses)


def _build_outer_order_by(resolved: ResolvedQuery) -> list[OrderByItem]:
    """Build ORDER BY using dimension/measure alias names for the outer CTE query."""
    from orionbelt.ast.nodes import Literal
    from orionbelt.compiler.star import _nulls_last

    col_to_dim: dict[tuple[str, str | None], str] = {
        (d.source_column, d.object_name): d.name for d in resolved.dimensions
    }
    order_by: list[OrderByItem] = []
    for expr, desc, nulls in resolved.order_by_exprs:
        nl = _nulls_last(nulls)
        if isinstance(expr, Literal):
            order_by.append(OrderByItem(expr=expr, desc=desc, nulls_last=nl))
        elif isinstance(expr, ColumnRef):
            dim_name = col_to_dim.get((expr.name, expr.table))
            name = dim_name if dim_name else expr.name
            order_by.append(OrderByItem(expr=ColumnRef(name=name), desc=desc, nulls_last=nl))
        else:
            # Measure expression — find matching measure by expression equality
            matched = False
            for m in resolved.measures:
                if m.expression == expr:
                    order_by.append(
                        OrderByItem(expr=ColumnRef(name=m.name), desc=desc, nulls_last=nl)
                    )
                    matched = True
                    break
            if not matched:
                order_by.append(OrderByItem(expr=expr, desc=desc, nulls_last=nl))
    return order_by
