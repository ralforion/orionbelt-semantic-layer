"""Wrapper CTEs for period-over-period (PoP) metrics.

Generates four CTEs using the synthetical date pattern:

| CTE           | Purpose                                                 |
|---------------|---------------------------------------------------------|
| date_range    | The extent of the time dimension over the query's rows  |
| date_spine    | Generate series with spine_date / spine_date_prev       |
| pop_base      | Aggregate measures onto the spine, every period kept    |
| pop_compare   | Self-join pop_base via spine_date_prev for comparison   |

The wrapper follows the same CTE pattern as ``total_wrap.py`` and
``cumulative_wrap.py``: the planner output is restructured into a
date-spine-driven query, and the comparison layer is added on top.

``date_range`` and ``pop_base`` read their rows through one derived table,
:func:`_source_tree_sql`, which is the query's own join tree filtered by the
query's own filters. Building it once is what keeps the two CTEs agreeing about
which rows the query is over, and what lets the spine join a plain column rather
than an expression over tables it has not joined yet.
"""

from __future__ import annotations

from collections.abc import Callable
from textwrap import indent
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    RawSQL,
    Select,
)
from orionbelt.compiler.expr_rewrite import collect_column_refs, map_column_refs
from orionbelt.compiler.having_hoist import windowed_aliases
from orionbelt.compiler.metric_expansion import expand_metric_expression
from orionbelt.compiler.outer_order_by import outer_order_by
from orionbelt.compiler.resolution import (
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
)
from orionbelt.compiler.type_resolver import (
    apply_exact_integer_avg,
    apply_exact_integer_sum,
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
    return cast_measure_to_resolved_type(expr, measure, model.settings, dialect, model)


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

    Both rewrites apply. Only the average was applied at first, so a query
    selecting a period-over-period metric *and* a cumulative one over the same
    integer ``SUM`` put a raw ``SUM(qty)`` in ``pop_base`` for the cumulative
    placeholder - the 64-bit accumulator this exists to avoid, reached by
    composing two wrappers rather than by using either alone.
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
    expr = apply_exact_integer_avg(expr, base, model.settings, dialect, model)
    return apply_exact_integer_sum(expr, base, dialect)


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
    _reject_multi_fact(resolved, pop_measures)

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
        resolved, model, dialect, qualify_table, grain, time_dim_name
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
    order_by = outer_order_by(resolved, model)

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
        order_by=order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _reject_multi_fact(resolved: ResolvedQuery, pop_measures: list[ResolvedMeasure]) -> None:
    """Refuse a period-over-period metric in a query the CFL planner unioned.

    This wrapper does not read the plan it wraps: it rebuilds a FROM of its own
    from ``resolved.join_steps``, around a date spine, and that shape holds one
    join tree. Given two independent facts it applied *every* leg's joins to
    *every* leg and emitted `LEFT JOIN "calendar" ON "Returns"."returned_on" =
    "Calendar"."day"` inside a leg selecting from ``sales``, leaving the
    composite CTE the planner had already built declared and never referenced.
    The database rejected it by naming a data object from the model, so it read
    as a modelling defect (#366).

    ``composite_cte`` rather than ``requires_cfl``, because the CFL planner
    delegates back to the star planner whenever the measures turn out to reach a
    single leg: what makes this unbuildable is a union that was actually
    produced, not one that was asked for.

    Supporting the shape means the spine joining a source that is itself a
    ``UNION ALL`` with NULL padding per leg. That is real work, and this is the
    other two multi-fact-plus-wrapper combinations' answer already: ``passes.py``
    declares grain dedup incompatible with this pass, and totals declares itself
    incompatible with it. Multi-fact CFL was the one that was neither supported
    nor refused.
    """
    if resolved.composite_cte is None:
        return
    facts = ", ".join(f"'{name}'" for name in resolved.fact_tables)
    metric = pop_measures[0].name if pop_measures else "period-over-period"
    raise ResolutionError(
        [
            SemanticError(
                code="INVALID_METRIC",
                message=(
                    f"Period-over-period metric '{metric}' cannot be combined with "
                    f"measures from independent facts ({facts}) in one query. The "
                    "comparison is built around a date spine over a single join "
                    "tree, and those measures are stacked into a UNION ALL that "
                    "has no such tree."
                ),
                path="metrics",
                hint=(
                    "Query the period-over-period metric with measures from its own "
                    "fact, and the other facts' measures separately."
                ),
                context={
                    "metric": metric,
                    "factTables": list(resolved.fact_tables),
                    "compositeCte": resolved.composite_cte,
                },
            )
        ]
    )


def _time_column_sql(time_dim: ResolvedDimension, model: SemanticModel, dialect: Dialect) -> str:
    """The SQL that reads a PoP metric's time dimension.

    Through ``make_column_expr`` rather than the dimension's physical column
    name, for the reason the projection goes through it: a computed dimension
    has no physical name, and quoting the empty string produced
    ``MIN("Event".""``) in the date-range scan and the same in the spine join.
    A computed column is as valid a time dimension as any other, and the model
    that declares one validates clean.

    Rendered inside the source tree, where every object the expression names is
    joined, so it may read more than its own data object.
    """
    return dialect.compile_expr(make_column_expr(model, time_dim.object_name, time_dim.column_name))


#: The derived table both PoP CTEs read their source rows from, and the column
#: it projects the time dimension's bucket as. Prefixed like every other name
#: the compiler invents, so it cannot collide with a modelled alias.
_SRC_ALIAS = "__ob_pop_src"
_BUCKET_COLUMN = "__ob_bucket"


def _flat_alias(table: str | None, name: str, taken: dict[str, tuple[str | None, str]]) -> str:
    """A name for ``table.name`` that survives being projected through a subquery.

    Two objects can carry the same column code, so the object is part of the
    name. The suffix is for the collision that spelling leaves - an object
    literally named ``A__b`` beside ``A`` with a column ``b`` - which is
    astronomically unlikely and silently wrong if it happens.
    """
    base = f"{table}__{name}" if table else name
    candidate = base
    index = 2
    while candidate in taken and taken[candidate] != (table, name):
        candidate = f"{base}_{index}"
        index += 1
    taken[candidate] = (table, name)
    return candidate


def _bucket_sql(
    time_dim: ResolvedDimension, model: SemanticModel, dialect: Dialect, grain: str
) -> str:
    """The time dimension, truncated to the spine's bucket size."""
    return dialect.render_date_trunc_sql(_time_column_sql(time_dim, model, dialect), grain)


def _source_tree_sql(
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    projections: list[str],
) -> str:
    """The query's own join tree, as a subquery projecting *projections*.

    One tree rooted at the base object, joined by ``resolved.join_steps`` and
    filtered by ``resolved.where_filters``: the same rows the star planner reads
    for the same query. Both PoP CTEs read from it - ``date_range`` scans it for
    the extent of the time dimension, ``pop_base`` joins the date spine to it -
    and building it once is what keeps those two from disagreeing about which
    rows the query is over. The filters used to reach only the first, so the
    spine covered the filtered range while the measures aggregated every row
    (#365).

    It also puts the tree *below* the spine join rather than beside it, which is
    what lets the time dimension be an expression over more than its own object:
    the bucket is computed here, where everything it names is in scope, and the
    spine joins a plain column (#358 review).
    """
    root = resolved.base_object or next(iter(resolved.required_objects), "")
    root_obj = model.data_objects[root]
    clauses = [f"  FROM {qualify_table(root_obj)} AS {dialect.quote_identifier(root)}"]

    joined = {root}
    for step in resolved.join_steps:
        to_obj = model.data_objects.get(step.to_object)
        if to_obj is None or step.to_object in joined:
            continue
        to_alias = dialect.quote_identifier(step.to_object)
        from_alias = dialect.quote_identifier(step.from_object)
        on_parts = []
        for fc, tc in zip(step.from_columns, step.to_columns, strict=True):
            fc_code = _resolve_col_code(model, step.from_object, fc)
            tc_code = _resolve_col_code(model, step.to_object, tc)
            on_parts.append(
                f"{from_alias}.{dialect.quote_identifier(fc_code)}"
                f" = {to_alias}.{dialect.quote_identifier(tc_code)}"
            )
        clauses.append(
            f"  LEFT JOIN {qualify_table(to_obj)} AS {to_alias} ON {' AND '.join(on_parts)}"
        )
        joined.add(step.to_object)

    if resolved.where_filters:
        predicates = " AND ".join(
            dialect.compile_expr(rf.expression) for rf in resolved.where_filters
        )
        clauses.append(f"  WHERE {predicates}")

    select_clause = ",\n       ".join(projections)
    return "SELECT " + select_clause + "\n" + "\n".join(clauses)


def _build_date_range_sql(
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    grain: str,
    time_dim_name: str,
) -> str:
    """Build the raw SQL body for the date_range CTE.

    The extent of the time dimension over the rows the query asks for, which is
    what the spine is generated across.

    ``MIN`` of the bucket rather than the bucket of ``MIN``: truncation is
    monotonic, so the two are the same value, and reading the column the source
    already projects keeps this and ``pop_base`` reading one definition of the
    bucket.
    """
    time_dim = next(d for d in resolved.dimensions if d.name == time_dim_name)
    bucket_q = dialect.quote_identifier(_BUCKET_COLUMN)
    src_q = dialect.quote_identifier(_SRC_ALIAS)
    source = _source_tree_sql(
        resolved,
        model,
        dialect,
        qualify_table,
        [f"{_bucket_sql(time_dim, model, dialect, grain)} AS {bucket_q}"],
    )
    bucket_ref = f"{src_q}.{bucket_q}"
    return (
        f"SELECT MIN({bucket_ref}) AS min_date,\n"
        f"       MAX({bucket_ref}) AS max_date\n"
        f"  FROM (\n{indent(source, '    ')}\n  ) AS {src_q}"
    )


def _build_pop_base_sql(
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str],
    grain: str,
    time_dim_name: str,
) -> str:
    """Build the raw SQL body for the pop_base CTE.

    Every period the spine carries, with the query's dimensions and measures
    aggregated onto it - including the periods with no rows, which is the whole
    point of the spine and why the join runs this way round.

    The source rows arrive through one derived table rather than a chain of
    joins hung off the spine. That is what makes the bucket a plain column in
    the ON, so the time dimension may be an expression over more than one object
    (#358 review), and what puts the query's filters where the measures can see
    them (#365).
    """
    spine_cte = dialect.quote_identifier("date_spine")
    src_q = dialect.quote_identifier(_SRC_ALIAS)
    time_dim = next(d for d in resolved.dimensions if d.name == time_dim_name)

    # 1. What this CTE projects, as expressions still naming the model's objects.
    dim_entries: list[tuple[str, Expr | None]] = []
    dim_groups: list[str] = []
    for d_idx, dim in enumerate(resolved.dimensions, 1):
        dim_groups.append(str(d_idx))
        if dim.name == time_dim_name:
            # ``None`` marks the spine's own column: a period with no rows has
            # to keep its date, which is the whole point of joining this way
            # round.
            dim_entries.append((dim.name, None))
            continue
        # Through ``make_column_expr``, because a dimension is not always a
        # column: a computed one has no physical name at all.
        dim_expr: Expr = make_column_expr(model, dim.object_name, dim.column_name)
        if dim.grain:
            # The grain is the dimension: without it this CTE groups by the raw
            # value and two rows of the same month stay two rows, under a column
            # labelled by the month.
            dim_expr = dialect.render_time_grain(dim_expr, dim.grain)
        dim_entries.append((dim.name, dim_expr))

    measure_entries: list[tuple[str, Expr]] = []
    seen: set[str] = set()

    def _add_measure(alias: str, expr: Expr) -> None:
        # A PoP metric and a plain measure can name the same component, and the
        # projection carries it once.
        if alias not in seen:
            seen.add(alias)
            measure_entries.append((alias, expr))

    for m in resolved.measures:
        if m.is_pop:
            # A PoP metric is assembled in ``pop_compare`` from the base
            # measure's value, so what this CTE owes it is that measure.
            for comp_name in m.component_measures:
                comp = resolved.metric_components.get(comp_name)
                if comp:
                    _add_measure(
                        comp_name, _apply_measure_cast(comp.expression, comp_name, model, dialect)
                    )
            continue
        # A metric rides along as its components' aggregates, not as its
        # formula: the formula's placeholders are the *aliases* of columns this
        # same SELECT list is producing, which only the dialects with lateral
        # alias references would resolve - and a nested derived metric names a
        # column nothing projects at all.
        expr = (
            expand_metric_expression(
                m.expression, resolved.metric_components, lambda comp: comp.expression
            )
            if m.component_measures
            else m.expression
        )
        # Same cast the star planner applies, so a measure keeps its declared
        # type whether or not the query also has a PoP metric.
        #
        # A *wrapper* metric is excluded. Its column here is only a placeholder
        # holding the base measure's aggregate until ``window_wrap`` /
        # ``cumulative_wrap`` builds the real window call, so the metric's own
        # dataType does not describe it yet. Casting it early corrupts the
        # input: a rank declaring ``dataType: integer`` truncated
        # ``SUM(amount)`` to INT, so 1.49 and 1.40 both became 1 and ranked
        # equal. Those wrappers apply the metric's type to the finished window
        # value themselves.
        #
        # The *rewrite* is a separate matter from the cast and does apply here:
        # an integer AVG has to reach the window already exact, or the wrapper
        # carries a raw floating average that no later cast can repair.
        is_wrapper_metric = m.is_window or m.is_cumulative
        if m.component_measures and not is_wrapper_metric:
            expr = _apply_metric_cast(expr, m.name, model, dialect)
        elif not m.component_measures:
            expr = _apply_measure_cast(expr, m.name, model, dialect)
        elif is_wrapper_metric:
            expr = _apply_base_measure_rewrite(expr, m.name, model, dialect)
        _add_measure(m.name, expr)

    # 2. The columns those expressions read, which the source has to carry out.
    refs: list[ColumnRef] = []
    for _, projection in dim_entries:
        if projection is not None:
            collect_column_refs(projection, refs)
    for _, measure_expr in measure_entries:
        collect_column_refs(measure_expr, refs)

    taken: dict[str, tuple[str | None, str]] = {}
    alias_of: dict[tuple[str, str], str] = {}
    projections = [
        f"{_bucket_sql(time_dim, model, dialect, grain)} AS "
        f"{dialect.quote_identifier(_BUCKET_COLUMN)}"
    ]
    for ref in refs:
        if ref.table is None or (ref.table, ref.name) in alias_of:
            continue
        alias = _flat_alias(ref.table, ref.name, taken)
        alias_of[(ref.table, ref.name)] = alias
        plain = ColumnRef(name=ref.name, table=ref.table)
        projections.append(f"{dialect.compile_expr(plain)} AS {dialect.quote_identifier(alias)}")

    def _repoint(expr: Expr) -> Expr:
        """Read the same value from the derived table's projection."""

        def _one(ref: ColumnRef) -> Expr:
            flat = alias_of.get((ref.table, ref.name)) if ref.table else None
            return ColumnRef(name=flat, table=_SRC_ALIAS) if flat else ref

        return map_column_refs(expr, _one)

    # 3. The CTE, reading its rows through one derived table.
    selects: list[str] = []
    for alias, dim_projection in dim_entries:
        if dim_projection is None:
            selects.append(f"{spine_cte}.spine_date AS {dialect.quote_identifier(alias)}")
        else:
            selects.append(
                f"{dialect.compile_expr(_repoint(dim_projection))} AS "
                f"{dialect.quote_identifier(alias)}"
            )
    for alias, expr in measure_entries:
        selects.append(
            f"{dialect.compile_expr(_repoint(expr))} AS {dialect.quote_identifier(alias)}"
        )

    source = _source_tree_sql(resolved, model, dialect, qualify_table, projections)
    bucket_ref = f"{src_q}.{dialect.quote_identifier(_BUCKET_COLUMN)}"
    return (
        "SELECT " + ",\n       ".join(selects) + "\n"
        f"  FROM {spine_cte}\n"
        f"  LEFT JOIN (\n{indent(source, '    ')}\n  ) AS {src_q}\n"
        f"    ON {bucket_ref} = {spine_cte}.spine_date\n"
        f"  GROUP BY {', '.join(dim_groups)}"
    )


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
