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
    ColumnRef,
    Expr,
    From,
    OrderByItem,
    RawSQL,
    Select,
)
from orionbelt.compiler.resolution import ResolutionError, ResolvedMeasure, ResolvedQuery
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import PeriodOverPeriodComparison, SemanticModel

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import DataObject


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

    # v1 constraint: all PoP metrics must share the same time dimension + grain
    pop_config = pop_measures[0]
    if pop_config.pop_time_dimension is None:
        raise ResolutionError([SemanticError(
            code="INVALID_METRIC",
            message="PoP metric missing required timeDimension",
            path="metrics",
        )])
    if pop_config.pop_grain is None:
        raise ResolutionError([SemanticError(
            code="INVALID_METRIC",
            message="PoP metric missing required grain",
            path="metrics",
        )])
    if pop_config.pop_offset_grain is None:
        raise ResolutionError([SemanticError(
            code="INVALID_METRIC",
            message="PoP metric missing required offsetGrain",
            path="metrics",
        )])

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
    date_range_cte = CTE(name="date_range", query=RawSQL(sql=date_range_sql))

    # --- CTE 2: date_spine ---
    # Use scalar subqueries so every dialect can resolve date_range references
    # without needing date_range in their FROM clause (universally compatible).
    spine_sql = dialect.render_date_spine_cte_sql(
        min_date="(SELECT min_date FROM date_range)",
        max_date="(SELECT max_date FROM date_range)",
        grain=grain,
        offset=offset,
        offset_grain=offset_grain,
    )
    date_spine_cte = CTE(name="date_spine", query=RawSQL(sql=spine_sql))

    # --- CTE 3: pop_base ---
    # Build FROM date_spine with LEFT JOINs to fact and dimension tables
    # Re-use the planner's join structure but restructured
    pop_base_sql = _build_pop_base_sql(
        ast, resolved, model, dialect, qualify_table, grain, time_obj_name, time_source_col
    )
    pop_base_cte = CTE(name="pop_base", query=RawSQL(sql=pop_base_sql))

    # --- CTE 4: pop_compare ---
    pop_compare_sql = _build_pop_compare_sql(resolved, dialect, pop_measures)
    pop_compare_cte = CTE(name="pop_compare", query=RawSQL(sql=pop_compare_sql))

    # --- Final SELECT from pop_compare ---
    outer_columns: list[Expr] = []
    for dim in resolved.dimensions:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))
    for m in resolved.measures:
        if not m.is_pop:
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))
        else:
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))

    # Remap ORDER BY to alias-only refs (dimension/measure names, not physical codes)
    outer_order_by = _build_outer_order_by(resolved)

    # Collect all CTEs (planner CTEs + our 4 new ones)
    all_ctes = list(ast.ctes) + [date_range_cte, date_spine_cte, pop_base_cte, pop_compare_cte]

    return Select(
        columns=outer_columns,
        from_=From(source="pop_compare", alias="pop_compare"),
        joins=[],
        where=None,
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
    # Dimension aliases: d1 = time dim (spine_date), d2..dN = others
    dim_selects: list[str] = []
    dim_groups: list[str] = []

    for d_idx, dim in enumerate(resolved.dimensions, 1):
        pop_measure = next((m for m in resolved.measures if m.is_pop), None)
        if pop_measure and dim.name == pop_measure.pop_time_dimension:
            dim_selects.append(f"date_spine.spine_date AS {dialect.quote_identifier(dim.name)}")
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
                    expr_sql = dialect.compile_expr(comp.expression)
                    measure_selects.append(f"{expr_sql} AS {dialect.quote_identifier(comp_name)}")
        else:
            expr_sql = dialect.compile_expr(m.expression)
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
    from_clause = "date_spine"
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
        f"\n  LEFT JOIN {time_table} AS {time_alias_q}\n    ON {trunc_col} = date_spine.spine_date"
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

    Self-joins pop_base via date_spine.spine_date_prev for period comparison.
    """
    # Dimension projections from pop_base
    selects: list[str] = []
    for dim in resolved.dimensions:
        q = dialect.quote_identifier(dim.name)
        selects.append(f"pop_base.{q} AS {q}")

    # Non-PoP measures: pass through
    for m in resolved.measures:
        if not m.is_pop:
            q = dialect.quote_identifier(m.name)
            selects.append(f"pop_base.{q} AS {q}")

    # PoP measures: compute comparison expression
    for m in pop_measures:
        if m.pop_comparison is None:
            raise ResolutionError([SemanticError(
                code="INVALID_METRIC",
                message=f"PoP measure '{m.name}' missing comparison type",
                path="metrics",
            )])
        # For single-measure PoP: use the base measure name
        base_name = m.pop_base_measure or m.component_measures[0]
        q_base = dialect.quote_identifier(base_name)
        q_metric = dialect.quote_identifier(m.name)

        current = f"pop_base.{q_base}"
        prev = f"prev.{q_base}"
        nullif_prev = f"NULLIF({prev}, 0)"

        if m.pop_comparison == PeriodOverPeriodComparison.RATIO:
            expr = f"{current} / {nullif_prev}"
        elif m.pop_comparison == PeriodOverPeriodComparison.DIFFERENCE:
            expr = f"{current} - {prev}"
        elif m.pop_comparison == PeriodOverPeriodComparison.PREVIOUS_VALUE:
            expr = prev
        elif m.pop_comparison == PeriodOverPeriodComparison.PERCENT_CHANGE:
            expr = f"{current} / {nullif_prev} - 1"
        else:
            raise ResolutionError([SemanticError(
                code="INVALID_METRIC",
                message=f"Unknown PoP comparison type: {m.pop_comparison}",
                path="metrics",
            )])

        selects.append(f"{expr} AS {q_metric}")

    select_clause = ",\n       ".join(selects)

    # Build the dimension-matching ON clause for the self-join
    # Match all non-time dimensions
    pop_time_dim = pop_measures[0].pop_time_dimension
    dim_matches: list[str] = []
    for dim in resolved.dimensions:
        if dim.name == pop_time_dim:
            continue
        q = dialect.quote_identifier(dim.name)
        dim_matches.append(f"pop_base.{q} = prev.{q}")

    # The time dimension matches via the spine's spine_date_prev
    time_q = dialect.quote_identifier(pop_time_dim or "")
    on_parts = [f"date_spine.spine_date_prev = prev.{time_q}"]
    on_parts.extend(dim_matches)
    on_clause = " AND ".join(on_parts)

    return (
        f"SELECT {select_clause}\n"
        f"  FROM pop_base\n"
        f"  LEFT JOIN date_spine ON pop_base.{time_q} = date_spine.spine_date\n"
        f"  LEFT JOIN pop_base AS prev\n"
        f"    ON {on_clause}"
    )


def _build_outer_order_by(resolved: ResolvedQuery) -> list[OrderByItem]:
    """Build ORDER BY using dimension/measure alias names for the outer CTE query."""
    from orionbelt.ast.nodes import Literal

    col_to_dim: dict[tuple[str, str | None], str] = {
        (d.source_column, d.object_name): d.name for d in resolved.dimensions
    }
    order_by: list[OrderByItem] = []
    for expr, desc in resolved.order_by_exprs:
        if isinstance(expr, Literal):
            order_by.append(OrderByItem(expr=expr, desc=desc))
        elif isinstance(expr, ColumnRef):
            dim_name = col_to_dim.get((expr.name, expr.table))
            name = dim_name if dim_name else expr.name
            order_by.append(OrderByItem(expr=ColumnRef(name=name), desc=desc))
        else:
            # Measure expression — find matching measure by expression equality
            matched = False
            for m in resolved.measures:
                if m.expression == expr:
                    order_by.append(OrderByItem(expr=ColumnRef(name=m.name), desc=desc))
                    matched = True
                    break
            if not matched:
                order_by.append(OrderByItem(expr=expr, desc=desc))
    return order_by
