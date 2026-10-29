"""Wrapper CTE for total and grain-override measures.

When a measure has ``total: true`` or a ``grain`` override, its value is
computed over more rows than one group's. Two ways, by aggregation:

* **A window over the grouped rows**, ``AGG(x) OVER (PARTITION BY ...)``, for
  the aggregations whose total is a combination of per-group values (below).
  For grand totals the partition is empty (``OVER ()``); for grain overrides
  it holds the effective grain dimensions. Window functions cannot coexist with
  ``GROUP BY`` on pre-grouped rows, so the planner output becomes a CTE and the
  windows run in an outer query.
* **A CTE of its own** for every other aggregation, and for every total under
  ``rollup`` or ``cube``: the measure computed again over the plan's own rows,
  grouped by the total's grain (nothing, for a grand total), and joined back
  (:func:`computed_apart`). A standard deviation, a distinct count or a median
  of the whole is not a combination of the groups' values, and a window over
  ``rollup`` rows also adds the subtotal rows.

Re-aggregation by window:

| Original | Window re-aggregation    | Notes                       |
|----------|--------------------------|-----------------------------|
| SUM      | SUM(x) OVER (...)        | sum of per-group sums       |
| COUNT    | SUM(x) OVER (...)        | sum of per-group counts     |
| MIN      | MIN(x) OVER (...)        | min of per-group mins       |
| MAX      | MAX(x) OVER (...)        | max of per-group maxes      |
| AVG      | SUM(s)/SUM(c) OVER (...) | exact via sum+count helpers |
"""

from __future__ import annotations

from collections.abc import Callable, Container
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
    IsNull,
    Join,
    JoinType,
    Literal,
    Select,
    Unnest,
    WindowFunction,
)
from orionbelt.compiler.having_hoist import apply_having_hoist, grouped_predicates
from orionbelt.compiler.metric_expansion import expand_metric_expression, metric_leaf_components
from orionbelt.compiler.outer_order_by import remap_order_by
from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
from orionbelt.models.semantic import SemanticModel

# The aggregations a window over the grouped rows re-aggregates exactly, and
# the window function that does it. ``AVG`` is the sum of its per-group sums
# over the sum of its counts (:func:`is_avg_total`).
_WINDOW_REAGGREGATION = {"SUM": "SUM", "COUNT": "SUM", "MIN": "MIN", "MAX": "MAX", "AVG": "SUM"}


def _reagg_func(aggregation: str) -> str:
    """Return the outer window function name for a given aggregation."""
    return _WINDOW_REAGGREGATION[aggregation.upper()]


def computed_apart(measure: ResolvedMeasure, resolved: ResolvedQuery) -> bool:
    """Whether a total or grain override is computed in a CTE of its own.

    A window over the grouped rows combines per-group values, which is the
    total only for a sum, a count, a minimum, a maximum and an average (from
    per-group sums and counts), each over all its values: summed per region, a
    standard deviation answered the sum of the regions' standard deviations
    and a distinct count counted a value once per region it appeared in. Under
    ``rollup`` or ``cube`` the grouped rows include the subtotal rows, so even
    a sum's total added each row once per level (70 came out 210). Such a
    measure is computed again over the plan's own rows instead, grouped by
    its grain, which gives every aggregation its exact value.
    """
    if not _needs_window_wrap(measure, resolved.dedup_computed):
        return False
    return (
        resolved.grouping is not None
        or measure.aggregation.upper() not in _WINDOW_REAGGREGATION
        or _aggregates_distinct(measure.expression)
    )


def _aggregates_distinct(expr: Expr) -> bool:
    """Whether *expr* holds an aggregate over distinct values (``SUM(DISTINCT x)``)."""
    if isinstance(expr, FunctionCall) and expr.distinct:
        return True
    if not is_dataclass(expr):
        return False
    for field in fields(expr):
        value = getattr(expr, field.name)
        for child in value if isinstance(value, (list, tuple)) else (value,):
            if (
                is_dataclass(child)
                and not isinstance(child, type)
                and _aggregates_distinct(cast(Expr, child))
            ):
                return True
    return False


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


def _peel_cast(expr: Expr) -> tuple[Expr, Callable[[Expr], Expr]]:
    """The planner's cast of a base-CTE column, and how to put it back.

    The base CTE feeds a window that re-aggregates the column, so the cast
    cannot stay inside it either: a measure declared ``dataType: integer``
    over three groups of 0.40 summed three zeros, 0 for the 1 its total rounds
    to, and a metric over it read the rounded column only when the measure was
    selected beside it. The window adds the uncast values and the cast types
    the total, once. BigQuery rounds a decimal cast it cannot parameterise
    (``ROUND(CAST(...), scale)``); both forms come back as they were.
    """
    if isinstance(expr, Cast):
        cast_node = expr

        def recast(inner: Expr) -> Expr:
            return replace(cast_node, expr=inner)

        return cast_node.expr, recast
    if isinstance(expr, FunctionCall) and expr.name.upper() == "ROUND" and len(expr.args) == 2:
        rounded, (cast_arg, places) = expr, expr.args
        if isinstance(cast_arg, Cast):

            def reround(inner: Expr) -> Expr:
                return replace(rounded, args=[replace(cast_arg, expr=inner), places])

            return cast_arg.expr, reround
    return expr, _unchanged


def _unchanged(expr: Expr) -> Expr:
    return expr


def _component_value(comp: ResolvedMeasure, resolved: ResolvedQuery) -> Expr:
    """A metric component's aggregate, bare: no output cast, no default.

    A metric's formula reads its components uncast, as the planner inlines
    them; the default goes back on where the value is finally read. The
    multi-fact planner records its re-aggregations already cast
    (``CAST(AVG(DISTINCT x) AS INTEGER)``), so a component read from there was
    rounded before the metric used it (200 for 233.3), and a windowed one
    summed rounded groups (0 for 120). The cast and the default are taken
    off in whichever order they wrap the aggregate.
    """
    return _bare(resolved.projected_expressions.get(comp.name, comp.expression), comp)


def _free_alias(name: str, taken: set[str]) -> str:
    """*name*, prefixed with underscores until none of *taken* (casefolded) is it."""
    while name.casefold() in taken:
        name = f"_{name}"
    taken.add(name.casefold())
    return name


def _bare(value: Expr, measure: ResolvedMeasure) -> Expr:
    """*value* without the planner's cast and *measure*'s default, in either order."""
    while True:
        bare = _peel_default(_peel_cast(value)[0], measure)
        if bare == value:
            return value
        value = bare


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
    # Qualified: the outer column carrying this window is named like the base
    # column it reads, and ClickHouse resolves a bare name to a SELECT alias, so
    # a metric's ``SUM("Revenue") OVER ()`` beside the selected measure read
    # the alias and nested one window inside another.
    return WindowFunction(
        func_name=reagg,
        args=[ColumnRef(name=measure.name, table="base")],
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
    apart: dict[str, Expr],
    inputs: dict[str, str],
) -> Expr:
    """Walk a metric AST and replace ColumnRef placeholders.

    Non-total components → their bare base CTE column, named in *inputs* when
    the measure is also selected, with their default.
    Total components → WindowFunction (re-aggregation), or the column of the
    CTE that computes them apart (*apart*, :func:`computed_apart`).

    A nested derived metric is expanded in place, so a ``total: true`` component
    it wraps is still re-aggregated rather than read as a per-group value.
    """

    def value_of(comp: ResolvedMeasure) -> Expr:
        if comp.name in apart:
            return apart[comp.name]
        if _needs_window_wrap(comp, resolved.dedup_computed):
            return _build_total_window(comp, resolved.dedup_computed)
        return comp.with_default(ColumnRef(name=inputs.get(comp.name, comp.name)))

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

    total_ctes, apart, apart_components, total_joins = _total_ctes(ast, resolved)

    # --- Build base CTE columns from the planner's AST columns ---
    base_columns: list[Expr] = []
    # The cast each windowed measure's outer column puts back (:func:`_peel_cast`).
    recasts: dict[str, Callable[[Expr], Expr]] = {}
    # The base column a metric reads a selected, unwindowed component from.
    metric_inputs: dict[str, str] = {}
    projected_inputs: set[str] = set()
    selected_columns = {c.alias: c.expr for c in ast.columns if isinstance(c, AliasedExpr)}
    # Every name the base CTE may carry, casefolded as engines compare quoted
    # identifiers: a metric input named like a dimension read the dimension.
    taken = {
        name.casefold()
        for name in [
            *selected_columns,
            *(d.name for d in resolved.dimensions),
            *(m.name for m in resolved.measures),
            *resolved.metric_components,
        ]
    }
    # Track which component measures are already present as direct measures
    direct_measure_names = {m.name for m in resolved.measures if not m.component_measures}

    for col_node in ast.columns:
        alias = _get_alias(col_node)
        if alias and alias in decompose_metrics:
            # Replace metric column with its individual component columns
            metric = next(m for m in resolved.measures if m.name == alias)
            for comp in metric_leaf_components(metric, resolved.metric_components):
                if comp.name in apart_components or comp.name in projected_inputs:
                    continue  # computed apart, or projected for another metric already
                projected_inputs.add(comp.name)
                selected = comp.name in direct_measure_names
                if selected and _needs_window_wrap(comp, resolved.dedup_computed):
                    continue  # the selected measure's base column is bare already
                if is_avg_total(comp, resolved.dedup_computed):
                    # AVG total needs sum + count helper columns
                    comp_expr = resolved.projected_expressions.get(comp.name, comp.expression)
                    base_columns.append(_build_avg_helpers_base_col(comp, "sum", comp_expr))
                    base_columns.append(_build_avg_helpers_base_col(comp, "count", comp_expr))
                    continue
                # A metric reads its components bare, as the planner inlines
                # them. A selected measure's column is cast to its type, so the
                # metric gets a column of its own: reading the selected one
                # made the metric depend on what else was selected (an amount
                # cast to an integer turned 33.3% into 0%). An anchored
                # component's aggregate reads conformed subquery columns, which
                # this CTE's FROM and joins keep in scope.
                if selected:
                    # From the selected column, which an earlier pass (grain
                    # dedup) may have moved onto a CTE of its own.
                    name = metric_inputs[comp.name] = _free_alias(
                        f"__ob_input_{len(metric_inputs)}", taken
                    )
                    value = _bare(selected_columns[comp.name], comp)
                else:
                    name, value = comp.name, _component_value(comp, resolved)
                base_columns.append(AliasedExpr(expr=value, alias=name))
        elif alias in apart:
            continue  # computed apart, in :func:`_total_ctes`
        elif alias and _is_avg_window_wrap_by_name(alias, resolved):
            # AVG total/grain-override direct measure: replace with sum + count helpers
            measure = next(m for m in resolved.measures if m.name == alias)
            base_columns.append(_build_avg_helpers_base_col(measure, "sum", col_node))
            base_columns.append(_build_avg_helpers_base_col(measure, "count", col_node))
        elif alias and _is_window_wrapped_by_name(alias, resolved):
            # A window re-aggregates this column, so the planner's cast and the
            # declared default come off it here and go back on around the window.
            assert isinstance(col_node, AliasedExpr)
            measure = next(m for m in resolved.measures if m.name == alias)
            uncast, recasts[alias] = _peel_cast(col_node.expr)
            base_columns.append(AliasedExpr(expr=_peel_default(uncast, measure), alias=alias))
        else:
            base_columns.append(col_node)

    if not base_columns:
        # Only measures computed apart, and no dimensions: the base is still
        # the one row an aggregate over no groups returns.
        base_columns.append(
            AliasedExpr(
                expr=FunctionCall(name="COUNT", args=[Literal.number(1)]), alias="__ob_rows"
            )
        )

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
                metric_expr = _substitute_metric_refs(
                    m.expression, resolved, apart_components, metric_inputs
                )
                outer_columns.append(AliasedExpr(expr=metric_expr, alias=m.name))
            else:
                # Metric without total components: pass-through
                outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))
        elif m.name in apart:
            outer_columns.append(AliasedExpr(expr=apart[m.name], alias=m.name))
        elif _needs_window_wrap(m, resolved.dedup_computed):
            # Total or grain-override measure: window function
            window = _build_total_window(m, resolved.dedup_computed)
            outer_columns.append(
                AliasedExpr(expr=recasts.get(m.name, _unchanged)(window), alias=m.name)
            )
        else:
            # Regular measure: pass-through
            outer_columns.append(AliasedExpr(expr=ColumnRef(name=m.name), alias=m.name))

    # --- ORDER BY remapping: use CTE aliases instead of raw expressions ---
    outer_order_by = remap_order_by(ast.order_by, resolved, model)

    # --- Assemble final Select ---
    all_ctes = [*ast.ctes, base_cte, *total_ctes]

    outer = Select(
        columns=outer_columns,
        from_=_from_cte("base"),
        joins=total_joins,
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


def _total_ctes(
    ast: Select, resolved: ResolvedQuery
) -> tuple[list[CTE], dict[str, Expr], dict[str, Expr], list[Join | Unnest]]:
    """The CTEs computing the measures :func:`computed_apart` asks for.

    One per grain they are totalled at: the measures computed again over the
    plan's own FROM, joins and WHERE - the rows its groups were built from,
    before any HAVING, as a window over every group reads them - grouped by
    the grain's dimensions, or not at all for a grand total. A grand total is
    cross joined to every row; a grain override is joined on its dimensions,
    a NULL member to its own group.

    A selected measure is the plan's own column, cast to its type and
    defaulted. A metric reads its component's aggregate uncast, as the planner
    inlines it, with its default: a measure both selected and read by a metric
    gets a column for each, or selecting it would change the metric (a mean of
    7/3 cast to an integer made the metric 200 for 233.3). Returns the CTEs,
    the column each selected measure and each metric component is read from,
    and the joins.
    """
    projected = {c.alias: c.expr for c in ast.columns if isinstance(c, AliasedExpr)}
    # (selected or component, name) -> (grain, value)
    wanted: dict[tuple[bool, str], tuple[tuple[str, ...], Expr]] = {}
    for m in resolved.measures:
        if not m.component_measures and computed_apart(m, resolved):
            wanted[(True, m.name)] = (tuple(m.effective_grain or ()), projected[m.name])
        for comp in metric_leaf_components(m, resolved.metric_components):
            if (False, comp.name) not in wanted and computed_apart(comp, resolved):
                wanted[(False, comp.name)] = (
                    tuple(comp.effective_grain or ()),
                    comp.with_default(_component_value(comp, resolved)),
                )
    by_grain: dict[tuple[str, ...], list[tuple[tuple[bool, str], Expr]]] = {}
    for key, (grain, value) in wanted.items():
        by_grain.setdefault(grain, []).append((key, value))

    ctes: list[CTE] = []
    selected: dict[str, Expr] = {}
    components: dict[str, Expr] = {}
    joins: list[Join | Unnest] = []
    for n, (grain, values) in enumerate(sorted(by_grain.items())):
        name = f"__ob_total_{n}"
        keys = [projected[d] for d in grain]
        columns: list[Expr] = [
            AliasedExpr(expr=key, alias=f"__ob_grain_{k}") for k, key in enumerate(keys)
        ]
        for k, ((is_selected, measure), value) in enumerate(values):
            columns.append(AliasedExpr(expr=value, alias=f"__ob_value_{k}"))
            (selected if is_selected else components)[measure] = ColumnRef(
                name=f"__ob_value_{k}", table=name
            )
        ctes.append(
            CTE(
                name=name,
                query=Select(
                    columns=columns,
                    from_=ast.from_,
                    joins=ast.joins,
                    where=ast.where,
                    group_by=keys,
                ),
            )
        )
        on: Expr | None = None
        for k, dimension in enumerate(grain):
            match = _null_safe_eq(
                ColumnRef(name=dimension, table="base"),
                ColumnRef(name=f"__ob_grain_{k}", table=name),
            )
            on = match if on is None else BinaryOp(left=on, op="AND", right=match)
        joins.append(
            Join(
                join_type=JoinType.LEFT if grain else JoinType.CROSS, source=name, alias=name, on=on
            )
        )
    return ctes, selected, components, joins


def _null_safe_eq(left: Expr, right: Expr) -> Expr:
    """``left = right``, also when both are NULL: a NULL member is a group too."""
    return BinaryOp(
        left=BinaryOp(left=left, op="=", right=right),
        op="OR",
        right=BinaryOp(left=IsNull(expr=left), op="AND", right=IsNull(expr=right)),
    )


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
