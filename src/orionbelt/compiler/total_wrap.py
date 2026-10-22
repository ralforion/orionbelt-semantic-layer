"""Wrapper CTE for total and grain-override measures using window functions.

When a measure has ``total: true`` or a ``grain`` override, the per-group
aggregate must be re-aggregated via ``AGG(x) OVER (PARTITION BY ...)``.
For grand totals the partition is empty (``OVER ()``); for grain overrides
the partition contains the effective grain dimensions.

Because window functions cannot coexist with ``GROUP BY`` on pre-grouped
rows, we wrap the planner output in a CTE and apply window functions in
an outer query.

Re-aggregation mapping (outer window function per aggregation type):

| Original       | Window Re-agg            | Notes                          |
|----------------|--------------------------|--------------------------------|
| SUM            | SUM(x) OVER (...)        | sum of per-group sums          |
| COUNT          | SUM(x) OVER (...)        | sum of per-group counts        |
| COUNT_DISTINCT | SUM(x) OVER (...)        | approximation (may overcount)  |
| MIN            | MIN(x) OVER (...)        | min of per-group mins          |
| MAX            | MAX(x) OVER (...)        | max of per-group maxes         |
| AVG            | SUM(s)/SUM(c) OVER (...) | exact via sum+count helpers    |
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import fields, is_dataclass, replace
from typing import cast

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Literal,
    Select,
    WindowFunction,
)
from orionbelt.compiler.having_hoist import apply_having_hoist, grouped_predicates
from orionbelt.compiler.metric_expansion import expand_metric_expression, metric_leaf_components
from orionbelt.compiler.outer_order_by import remap_order_by
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.models.semantic import SemanticModel

_UNSUPPORTED_TOTAL_AGGS = frozenset(
    {
        "MEDIAN",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "FIRST",
        "LAST",
        "MODE",
        "LISTAGG",
        "ANY_VALUE",
    }
)


def _reagg_func(aggregation: str) -> str:
    """Return the outer window function name for a given aggregation."""
    agg = aggregation.upper()
    if agg in _UNSUPPORTED_TOTAL_AGGS:
        raise ValueError(
            f"Aggregation '{agg}' does not support total: true "
            "(cannot be re-aggregated via window functions)"
        )
    if agg in ("SUM", "COUNT", "COUNT_DISTINCT", "AVG"):
        return "SUM"
    if agg == "MIN":
        return "MIN"
    if agg == "MAX":
        return "MAX"
    return "SUM"


def _needs_window_wrap(measure: ResolvedMeasure, dedup_computed: Container[str]) -> bool:
    """Check if a measure needs window wrapping (total or grain override).

    Measures with ``filter_context`` are handled by ``filter_wrap`` and skipped
    here. Measures and metric components in *dedup_computed* are handled by
    ``compiler.grain_dedup``, which computes their total or grain override in a
    CTE at that grain — deduplicated, where a window over this pass's input
    cannot reproduce it, because the per-group values it would sum belong to
    overlapping groups and would double count; or a fact-side component of a
    metric that pass splits, whose fact tables this pass no longer sees. Both
    are pass-through as far as this wrapper is concerned.

    *dedup_computed* is required rather than defaulted: every caller has it, and
    a default is exactly how the exclusion came to be applied in one place and
    forgotten in the outer projection.
    """
    if measure.name in dedup_computed:
        return False
    if measure.filter_context is not None:
        return False
    return measure.total or measure.grain_override is not None


def is_avg_total(measure: ResolvedMeasure, dedup_computed: Container[str]) -> bool:
    """Whether a measure is an AVG total/grain-override, needing sum+count helpers.

    Public because it is also a *precondition*: the helpers decompose the
    aggregate's argument, so this measure cannot be window-wrapped over a value
    some earlier pass already averaged. ``compiler.passes`` refuses that.
    """
    return _needs_window_wrap(measure, dedup_computed) and measure.aggregation.upper() == "AVG"


def _avg_sum_alias(name: str) -> str:
    return f"{name}__sum"


def _avg_count_alias(name: str) -> str:
    return f"{name}__count"


def _partition_by_exprs(measure: ResolvedMeasure) -> list[Expr]:
    """Build PARTITION BY column refs from effective_grain (empty = grand total)."""
    if measure.effective_grain:
        return [ColumnRef(name=d) for d in measure.effective_grain]
    return []


def _peel_default(expr: Expr, measure: ResolvedMeasure) -> Expr:
    """Strip *measure*'s declared default off a base-CTE column.

    The base CTE feeds a window that re-aggregates the column, so the default
    cannot stay inside it: ``SUM(COALESCE(SUM(x), 5)) OVER ()`` adds 5 for every
    group that saw nothing, where the field says the *measure* reads 5 when it
    saw nothing. :func:`_build_total_window` puts it back around the window.

    Descends through the planner's cast, which stays where it is - it types the
    aggregate, not the default.
    """
    if measure.default_value is None:
        return expr
    if isinstance(expr, AliasedExpr):
        return replace(expr, expr=_peel_default(expr.expr, measure))
    if isinstance(expr, Cast):
        return replace(expr, expr=_peel_default(expr.expr, measure))
    if isinstance(expr, FunctionCall) and expr.name.upper() == "COALESCE" and len(expr.args) == 2:
        return expr.args[0]
    return expr


def _build_total_window(measure: ResolvedMeasure, dedup_computed: Container[str]) -> Expr:
    """Build a window function for a total/grain-override measure's outer column.

    This is where the measure's value is finally produced, so a declared default
    is re-applied here - :func:`_peel_default` took it off the column the window
    reads.
    """
    return measure.with_default(_build_total_window_expr(measure, dedup_computed))


def _build_total_window_expr(measure: ResolvedMeasure, dedup_computed: Container[str]) -> Expr:
    partition_by = _partition_by_exprs(measure)
    if is_avg_total(measure, dedup_computed):
        return BinaryOp(
            left=WindowFunction(
                func_name="SUM",
                args=[ColumnRef(name=_avg_sum_alias(measure.name))],
                partition_by=partition_by,
            ),
            op="/",
            right=WindowFunction(
                func_name="SUM",
                args=[ColumnRef(name=_avg_count_alias(measure.name))],
                partition_by=partition_by,
            ),
        )
    reagg = _reagg_func(measure.aggregation)
    return WindowFunction(
        func_name=reagg,
        args=[ColumnRef(name=measure.name)],
        partition_by=partition_by,
    )


def _collect_total_names(resolved: ResolvedQuery) -> set[str]:
    """Collect names of all measures that need window wrapping (direct + metric components).

    Measures handled by ``compiler.grain_dedup`` are excluded: it computes their
    total or grain override in a CTE deduplicated at that grain, which a window over this
    pass's input cannot reproduce. Those per-group values belong to overlapping
    groups, so ``SUM(...) OVER ()`` would double count.
    """
    names: set[str] = set()
    dedup = resolved.dedup_computed
    for m in resolved.measures:
        if _needs_window_wrap(m, dedup):
            names.add(m.name)
        for comp in metric_leaf_components(m, resolved.metric_components):
            if _needs_window_wrap(comp, dedup):
                names.add(comp.name)
    return names


def _metrics_with_total_components(resolved: ResolvedQuery) -> set[str]:
    """Identify metrics that reference at least one window-wrapped component."""
    names: set[str] = set()
    for m in resolved.measures:
        if not m.component_measures:
            continue
        for comp in metric_leaf_components(m, resolved.metric_components):
            if _needs_window_wrap(comp, resolved.dedup_computed):
                names.add(m.name)
                break
    return names


def _substitute_metric_refs(
    expr: Expr,
    resolved: ResolvedQuery,
    total_names: set[str],
) -> Expr:
    """Walk a metric AST and replace ColumnRef placeholders.

    Non-total components → ColumnRef (pass-through from base CTE).
    Total components → WindowFunction (re-aggregation).

    A nested derived metric is expanded in place, so a ``total: true`` component
    it wraps is still re-aggregated rather than read as a per-group value.
    """

    def value_of(comp: ResolvedMeasure) -> Expr:
        if _needs_window_wrap(comp, resolved.dedup_computed):
            return _build_total_window(comp, resolved.dedup_computed)
        return ColumnRef(name=comp.name)

    return expand_metric_expression(expr, resolved.metric_components, value_of)


def wrap_with_totals(
    ast: Select, resolved: ResolvedQuery, model: SemanticModel | None = None
) -> Select:
    """Wrap a planner AST with a CTE + outer query for total measures.

    If no totals are present, returns ``ast`` unchanged.
    """
    if not resolved.has_totals:
        return ast

    total_names = _collect_total_names(resolved)
    decompose_metrics = _metrics_with_total_components(resolved)

    if not total_names and not decompose_metrics:
        return ast

    # --- Build base CTE columns from the planner's AST columns ---
    base_columns: list[Expr] = []
    # Track which component measures are already present as direct measures
    direct_measure_names = {m.name for m in resolved.measures if not m.component_measures}

    for col_node in ast.columns:
        alias = _get_alias(col_node)
        if alias and alias in decompose_metrics:
            # Replace metric column with its individual component columns
            metric = next(m for m in resolved.measures if m.name == alias)
            for comp in metric_leaf_components(metric, resolved.metric_components):
                if comp.name in direct_measure_names:
                    continue  # Already present as a direct measure
                if is_avg_total(comp, resolved.dedup_computed):
                    # AVG total needs sum + count helper columns
                    comp_expr = resolved.projected_expressions.get(comp.name, comp.expression)
                    base_columns.append(_build_avg_helpers_base_col(comp, "sum", comp_expr))
                    base_columns.append(_build_avg_helpers_base_col(comp, "count", comp_expr))
                else:
                    # An anchored component's aggregate reads conformed subquery
                    # columns. This CTE reuses the planner's FROM and joins, so
                    # those subqueries are in scope and the foreign fact is not.
                    base_columns.append(
                        _peel_default(
                            AliasedExpr(
                                expr=resolved.projected_expressions.get(comp.name, comp.expression),
                                alias=comp.name,
                            ),
                            comp,
                        )
                    )
        elif alias and _is_avg_window_wrap_by_name(alias, resolved):
            # AVG total/grain-override direct measure: replace with sum + count helpers
            measure = next(m for m in resolved.measures if m.name == alias)
            base_columns.append(_build_avg_helpers_base_col(measure, "sum", col_node))
            base_columns.append(_build_avg_helpers_base_col(measure, "count", col_node))
        elif alias and _is_window_wrapped_by_name(alias, resolved):
            # A window re-aggregates this column, so the declared default comes
            # off it here and goes back on around the window.
            measure = next(m for m in resolved.measures if m.name == alias)
            base_columns.append(_peel_default(col_node, measure))
        else:
            base_columns.append(col_node)

    # --- Build base CTE: planner's AST with modified columns, no ORDER BY/LIMIT ---
    # No HAVING: a total or grain override sums every group at its grain, so
    # the window has to see the groups a HAVING removes. Evaluated here, a
    # predicate on any other measure shrank the parent total to the groups it
    # kept. The predicates filter this wrapper's output instead, below.
    base_cte_query = Select(
        columns=base_columns,
        from_=ast.from_,
        joins=ast.joins,
        where=ast.where,
        group_by=ast.group_by,
        having=None,
        order_by=[],
        limit=None,
        offset=None,
        ctes=[],
        grouping=ast.grouping,
    )

    base_cte = CTE(name="base", query=base_cte_query)

    # --- Build outer SELECT ---
    outer_columns: list[Expr] = []

    # Dimensions: pass-through
    for dim in resolved.dimensions:
        outer_columns.append(AliasedExpr(expr=ColumnRef(name=dim.name), alias=dim.name))

    # Measures
    for m in resolved.measures:
        if m.component_measures:
            # Metric
            if m.name in decompose_metrics:
                # Rebuild expression with window functions for total components
                metric_expr = _substitute_metric_refs(m.expression, resolved, total_names)
                outer_columns.append(AliasedExpr(expr=metric_expr, alias=m.name))
            else:
                # Metric without total components: pass-through
                outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))
        elif _needs_window_wrap(m, resolved.dedup_computed):
            # Total or grain-override measure: window function
            outer_columns.append(
                AliasedExpr(expr=_build_total_window(m, resolved.dedup_computed), alias=m.name)
            )
        else:
            # Regular measure: pass-through
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))

    # --- ORDER BY remapping: use CTE aliases instead of raw expressions ---
    outer_order_by = remap_order_by(ast.order_by, resolved, model)

    # --- Assemble final Select ---
    all_ctes = list(ast.ctes) + [base_cte]

    outer = Select(
        columns=outer_columns,
        from_=_from_cte("base"),
        joins=[],
        where=None,
        group_by=[],
        having=None,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )
    if ast.having is None:
        return outer
    # The rows a later window wrapper sees are the filtered ones, as before:
    # only the total's own window reads past the HAVING.
    return apply_having_hoist(outer, grouped_predicates(resolved, model), cte_name="totals_having")


def _get_alias(expr: Expr) -> str | None:
    """Extract the alias from an AliasedExpr, or None."""
    if isinstance(expr, AliasedExpr):
        return expr.alias
    return None


def _from_cte(name: str) -> From:
    """Build a FROM clause referencing a CTE."""
    return From(source=name, alias=name)


def _is_avg_window_wrap_by_name(name: str, resolved: ResolvedQuery) -> bool:
    """Check if a direct measure with given name is an AVG total/grain-override."""
    for m in resolved.measures:
        if m.name == name and not m.component_measures:
            return is_avg_total(m, resolved.dedup_computed)
    return False


def _is_window_wrapped_by_name(name: str, resolved: ResolvedQuery) -> bool:
    """Check if a direct measure with given name is re-aggregated by a window."""
    for m in resolved.measures:
        if m.name == name and not m.component_measures:
            return _needs_window_wrap(m, resolved.dedup_computed)
    return False


def _averaged_argument(expr: Expr, helper: str) -> list[Expr] | None:
    """The argument the *helper* (``SUM`` or ``COUNT``) of an average reads.

    The first ``AVG``, or the first *helper*, met left to right through
    whatever wraps it: the column alias, the planner's cast, a declared
    default's ``COALESCE``, and the exact or double rewrites a dialect makes
    of the average itself. Those leave no ``AVG`` to read - ``SUM(CAST(x AS
    DECIMAL(38, 0))) / COUNT(x)`` on Dremio, integer arithmetic on DuckDB,
    ``CAST(SUM(DISTINCT x) AS FLOAT) / COUNT(DISTINCT x)`` on Snowflake - but
    each still sums and counts the argument, the sum through whatever widens
    its accumulator. Looking only through the wrappers found no aggregate
    under a division and fell back to ``SUM(1) / COUNT(1)``: the total of an
    integer average of 2, 4 and 6 read 1.
    """
    if isinstance(expr, FunctionCall) and expr.name.upper() in ("AVG", helper):
        return list(expr.args)
    if not is_dataclass(expr):
        return None
    for field in fields(expr):
        value = getattr(expr, field.name)
        for child in value if isinstance(value, (list, tuple)) else (value,):
            if is_dataclass(child) and not isinstance(child, type):
                found = _averaged_argument(cast(Expr, child), helper)
                if found is not None:
                    return found
    return None


def _build_avg_helpers_base_col(measure: ResolvedMeasure, kind: str, source: Expr) -> AliasedExpr:
    """Build a SUM or COUNT base CTE column for an AVG total measure.

    For AVG(expr), we need:
    - SUM(expr) AS "name__sum"
    - COUNT(expr) AS "name__count"

    *source* is the column the plan already projects the measure as, so its
    argument is whatever that plan actually reads: the fact's own column in a
    star, a conformed subquery's column when the measure is anchored, and the
    composite CTE's column under CFL - where the measure's own resolved
    expression still names a fact table this CTE does not select from. What it
    is not is the whole projected column: :func:`_averaged_argument` takes the cast
    and any declared default off first, since ``COALESCE(AVG(x), 0)``
    decomposed as it stands reads the default as a second argument and emits
    ``SUM(AVG(x), 0)``, which is not SQL. The default goes back on in
    :func:`_build_total_window`.
    """
    # Fallback: use Literal(1) — an AVG measure with no args is unusual
    # but using the measure alias as a column ref would be invalid SQL.
    inner_args = _averaged_argument(source, "SUM" if kind == "sum" else "COUNT") or [
        Literal.number(1)
    ]

    if kind == "sum":
        return AliasedExpr(
            expr=FunctionCall(name="SUM", args=inner_args),
            alias=_avg_sum_alias(measure.name),
        )
    else:
        return AliasedExpr(
            expr=FunctionCall(name="COUNT", args=inner_args),
            alias=_avg_count_alias(measure.name),
        )
