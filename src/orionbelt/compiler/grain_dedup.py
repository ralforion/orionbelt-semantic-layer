"""Grain deduplication for measures sourced from the *one* side of a join.

``compiler/fanout.py`` catches row multiplication that happens when a join is
traversed *backwards* (a reversed many-to-one, or a many-to-many). It treats a
*forward* many-to-one as safe, which is true for measures sourced from the many
side — the side that drives the query grain.

It is not true for a measure sourced from the **one** side. ``Sales`` joined to
``Products`` repeats each product row once per sale, so
``SUM(Products.stock_on_hand)`` grouped by ``Sales.region`` counts every product
once per sale it appeared in.

The query is refused outright when the one-side measure is the *only* measure —
resolution anchors the base object on the measure's source and the many side
becomes unreachable (``UNREACHABLE_REQUIRED_OBJECT``). But when a many-side
measure rides along, the base flips to the many side, the join is forward, and
the inflated ``SUM`` compiles silently. That is the case this module fixes.

The rewrite aggregates the affected measures in their own CTE, over rows
deduplicated on the source object's identity, then joins the result back onto
the main query's grain:

    WITH "main" AS (            -- many-side measures, at base grain
      SELECT region, SUM(s.amount) ... GROUP BY region
    ),
    "dedup_0" AS (              -- one-side measures, one row per (grain, product)
      SELECT "Region", SUM("__ob_c0") AS "Total Stock On Hand"
      FROM (
        SELECT DISTINCT s.region AS "Region",
               p.id AS "__ob_k0", p.stock_on_hand AS "__ob_c0"
        FROM sales s LEFT JOIN products p ON ...
      ) AS "dedup_src_0"
      GROUP BY "Region"
    )
    SELECT ... FROM "main" LEFT JOIN "dedup_0" USING (grain)

Note the arithmetic this produces: per-group values are correct, but a product
sold in two regions is counted in both, so the groups overlap and the column
does not sum to the product catalogue's grand total. That is inherent to the
question, not to the rewrite — it is the same caveat
:data:`~orionbelt.models.warnings.WarningCode.FAN_TRAP_RISK` already carries for
the junction-table case, and a warning says so.

Two things ride on that shape. A **metric** whose component needs dedup cannot
keep the planner's single inlined expression, so the pass computes every
component separately — each in whichever CTE suits it — and rebuilds the
metric's formula over those columns in the outer projection. And a **HAVING**
predicate on a deduplicated measure moves out to the outer query's ``WHERE``:
inside ``__ob_main`` the value does not exist yet, but the outer query is
already one row per query grain, so filtering it there means the same thing.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass, field, replace
from itertools import combinations
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Exists,
    Expr,
    From,
    FunctionCall,
    IsNull,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    Select,
    Star,
    UnionAll,
    Unnest,
)
from orionbelt.compiler.expr_rewrite import (
    collect_column_refs,
    collect_referenced_tables,
    map_column_refs,
    map_nodes,
    rewrite_column_refs,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.filters import (
    build_exists_filter_expr,
    build_measure_filter_condition,
    collect_measure_filter_objects,
)
from orionbelt.compiler.graph import JoinGraph, joined_beneath
from orionbelt.compiler.having_hoist import dimensions_to_aliases, windowed_aliases
from orionbelt.compiler.metric_expansion import (
    metric_leaf_components,
    metric_over_components,
)
from orionbelt.compiler.null_rejection import correlated_tables
from orionbelt.compiler.resolution import (
    ChildlessLeg,
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
    make_dimension_expr,
)
from orionbelt.compiler.star import grouping_flag_alias
from orionbelt.compiler.window_wrap import _ddm_window_components
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import FilterOperator, Grouping, QueryFilter, Subquery
from orionbelt.models.semantic import Cardinality, DataObject, Measure, SemanticModel
from orionbelt.models.warnings import WarningCode, warning

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect

# Aggregations whose result is unchanged when every input row is duplicated.
# Everything else — SUM, COUNT, AVG, the distribution/regression family,
# LISTAGG — reads row multiplicity and is wrong over replicated rows.
MULTIPLICITY_SAFE_AGGREGATIONS = frozenset(
    # A repeated row repeats its (key, value) pair, which leaves the greatest
    # and least pair where they were: first / last are as safe as min / max.
    {"min", "max", "count_distinct", "any_value", "first", "last"}
)

# Vendor-delegated aggregation: the engine resolves it through its own metric
# machinery, so we cannot reason about its multiplicity behaviour. Left alone.
_DELEGATED_AGGREGATION = "measure"

# Everything this pass introduces into the query namespace carries an ``__ob_``
# prefix. A CTE named ``main`` — which is what this pass used to emit — is a
# plausible schema or table name in a user's warehouse (it is DuckDB's default
# schema), so the generated SQL read as ``FROM "main"."sales"`` on one line and
# ``FROM "main"`` meaning the CTE on another. It resolved correctly, since a
# schema-qualified reference cannot be confused with a single-identifier CTE,
# but nothing about it was obvious to a reader debugging the output.
_KEY_PREFIX = "__ob_k"
_COL_PREFIX = "__ob_c"
_MAIN_CTE = "__ob_main"
_DEDUP_CTE = "__ob_dedup"
_DEDUP_SRC = "__ob_dedup_src"
_DEDUP_TOTAL_CTE = "__ob_dedup_total"
_DEDUP_GRAIN_CTE = "__ob_dedup_grain"
# A non-deduplicated component of a split metric, at its own grain.
_AT_GRAIN_CTE = "__ob_at_grain"
_AT_TOTAL_CTE = "__ob_at_total"
# The query's groups when parents without children add some ``main`` lacks.
_GROUPS_ALL_CTE = "__ob_groups_all"
_GROUPS_CTE = "__ob_groups"


class GrainDedupUnsupportedError(FanoutError):
    """A measure needs grain dedup but the query combines it with something the
    rewrite cannot express.

    Raised instead of emitting an inflated result. Subclasses
    :class:`~orionbelt.compiler.fanout.FanoutError` because it reports the same
    class of problem — row multiplication that would corrupt an aggregate — so
    every caller that already handles fanout handles this too.
    """


def measure_source_objects(measure: Measure) -> set[str]:
    """Data objects whose columns a measure reads.

    Thin delegator to :attr:`Measure.source_objects`, which is where this lives
    now that the parser needs it too and cannot import the compiler.
    """
    return measure.source_objects


def replicated_objects(resolved: ResolvedQuery) -> set[str]:
    """Objects whose rows the query's join path replicates.

    ``join_steps`` form a tree rooted at ``base_object``. Walking outward, an
    object's rows are replicated once the path reaching it crosses a *forward*
    many-to-one or many-to-many step: the many side sets the grain, so each row
    on the one side repeats per matching base row. Replication is inherited —
    anything joined beyond an already-replicated object is replicated too.

    Reversed steps are skipped: those are the classic fanout that
    :func:`~orionbelt.compiler.fanout.detect_fanout` already rejects, and
    claiming them here would report one problem twice.

    An **unnest** replicates the other way round. Its child is the many side —
    each element appears exactly once — and it is the *parent* whose row repeats
    once per element, carrying everything already on that row with it. So the
    query's whole join tree is replicated except the elements themselves, which
    is why a nested-side ``SUM`` is right as it stands while a parent-side one
    counts a charge once per label it happens to have.
    """
    replicated: set[str] = set()
    for step in resolved.join_steps:
        if step.nested:
            continue
        if step.reversed:
            continue
        multiplies = step.cardinality in (Cardinality.MANY_TO_ONE, Cardinality.MANY_TO_MANY)
        if multiplies or step.from_object in replicated:
            replicated.add(step.to_object)

    unnested = {step.to_object for step in resolved.join_steps if step.nested}
    if unnested:
        in_query = {resolved.base_object} | {
            name for step in resolved.join_steps for name in (step.from_object, step.to_object)
        }
        replicated |= in_query - unnested
    return replicated


@dataclass(frozen=True)
class DedupPlan:
    """Which measures the pass must aggregate over deduplicated rows.

    ``measures`` are named in ``select.measures``; ``components`` are reached
    only through a metric's expression. The two are kept apart because they are
    projected differently — a selected measure keeps the planner's own column,
    a component has to be split back out of the metric that inlined it.
    """

    measures: dict[str, str] = field(default_factory=dict)
    components: dict[str, str] = field(default_factory=dict)


def detect_dedup_measures(resolved: ResolvedQuery, model: SemanticModel) -> DedupPlan:
    """Map each measure needing dedup to the replicated object it is sourced from.

    A measure qualifies when **every** column it reads comes from a single
    replicated object. A measure that mixes a replicated column with a
    base-grain one (``{[Sales].[Quantity]} * {[Products].[List Price]}``) is evaluated
    per base row and is already correct, so it is left alone.

    ``allowFanOut: true`` opts out — the modeller has declared the duplication
    intentional.
    """
    if not resolved.join_steps:
        return DedupPlan()

    replicated = replicated_objects(resolved)
    if not replicated:
        return DedupPlan()

    effective = model.effective_measures
    measures: dict[str, str] = {}
    components: dict[str, str] = {}
    # Rows without children join the deduplicated rows of their object, so every
    # measure on it is deduplicated, even one duplication would not change.
    force = resolved.childless_leg is not None

    for resolved_measure in resolved.measures:
        if resolved_measure.component_measures:
            components.update(
                _metric_component_targets(
                    resolved_measure, resolved, effective, replicated, force=force
                )
            )
            continue

        target = _dedup_target(resolved_measure.name, effective, replicated, force=force)
        if target is not None:
            measures[resolved_measure.name] = target

    return DedupPlan(measures=measures, components=components)


def split_grain_components(resolved: ResolvedQuery) -> set[str]:
    """The components :attr:`ResolvedQuery.dedup_grain_components` names.

    A metric is split when it reads a component this pass computes. Its other
    components are then read from this pass's ``main`` CTE, which holds only
    the query-grain aggregates, so one carrying ``total: true`` or a ``grain``
    override has to be computed here too, at that grain. That in turn splits
    every other metric reading it, so the set is closed over the query's
    metrics. A filterContext component is computed by a scan of its own and is
    not one of them.
    """
    found: set[str] = set()
    while True:
        added = {
            c.name
            for metric in resolved.measures
            if is_split_metric(metric, resolved, found)
            for c in metric_leaf_components(metric, resolved.metric_components)
            if c.name not in resolved.dedup_targets
            and c.filter_context is None
            and (c.total or c.grain_override is not None)
        }
        if added <= found:
            return found
        found |= added


def is_split_metric(
    metric: ResolvedMeasure, resolved: ResolvedQuery, grain_components: Container[str]
) -> bool:
    """Whether the pass rebuilds *metric* from its components: it reads a
    deduplicated component or one of *grain_components*. A metric over a window
    metric is never rebuilt here: the window pass computes it, from the inputs
    this pass projects by name (:func:`_window_inputs`)."""
    if _ddm_window_components(metric, resolved.metric_components):
        return False
    return any(
        c.name in resolved.dedup_components or c.name in grain_components
        for c in metric_leaf_components(metric, resolved.metric_components)
    )


def _metric_component_targets(
    metric: ResolvedMeasure,
    resolved: ResolvedQuery,
    effective: dict[str, Measure],
    replicated: set[str],
    *,
    force: bool = False,
) -> dict[str, str]:
    """Components of *metric* that must be deduplicated, keyed by source object.

    A metric whose components all sit at the query grain is left whole. As soon
    as one needs dedup, :func:`wrap_with_grain_dedup` computes every component
    separately and rebuilds the metric expression over the results, so the
    inlined aggregate never reaches the replicated join.

    Nested *derived* metrics are followed, because they are expanded in place
    into the same expression. A window metric's base measure is deduplicated in
    its own right: the window pass reads it by name from this pass's output
    (:func:`_window_inputs`). A cumulative or period-over-period component is
    refused: that wrapper rebuilds its base measure's aggregate from the fact
    tables, which a dedup CTE cannot serve.
    """
    targets: dict[str, str] = {}
    for component in metric_leaf_components(metric, resolved.metric_components):
        if not component.component_measures:
            target = _dedup_target(component.name, effective, replicated, force=force)
            if target is not None:
                targets[component.name] = target
            continue

        # A wrapper-computed metric stands for the measure it windows over, so
        # that is what the replication applies to.
        base = (
            component.window_base_measure
            or component.cumulative_measure
            or component.pop_base_measure
        )
        target = None if base is None else _dedup_target(base, effective, replicated, force=force)
        if target is None or base is None:
            continue
        if component.window_base_measure is not None:
            targets[base] = target
            continue
        msg = (
            f"Metric '{metric.name}' references metric '{component.name}', whose measure "
            f"'{base}' is sourced from '{target}' — an object whose rows this query's "
            f"joins replicate. '{component.name}' is computed by its own wrapper, which "
            f"rebuilds that aggregate from the fact tables and so cannot read the "
            f"deduplicated value. Query '{component.name}' on its own, or set "
            f"allowFanOut: true on '{base}' if the duplication is intended."
        )
        raise GrainDedupUnsupportedError(msg)
    return targets


def _dedup_target(
    measure_name: str,
    effective: dict[str, Measure],
    replicated: set[str],
    *,
    force: bool = False,
) -> str | None:
    """The replicated object a measure must be deduplicated on, if any.

    *force* deduplicates a measure duplication would not change too (``min``,
    ``distinct``): the rows of its object without child rows are added beside
    the deduplicated ones, and only that path reads them.
    """
    measure = effective.get(measure_name)
    if measure is None or measure.allow_fan_out:
        return None

    # ``distinct: true`` renders ``AGG(DISTINCT x)``. Replicating rows does not
    # change the *set* of distinct values, so any such measure already reads
    # correctly over a replicating join — most commonly a
    # ``count`` + ``distinct`` over the parent key, counted from the child fact.
    if measure.distinct and not force:
        return None

    aggregation = measure.aggregation.lower()
    if aggregation == _DELEGATED_AGGREGATION or (
        aggregation in MULTIPLICITY_SAFE_AGGREGATIONS and not force
    ):
        return None

    sources = measure_source_objects(measure)
    if not sources or not sources <= replicated:
        return None

    if len(sources) > 1:
        joined = ", ".join(f"'{s}'" for s in sorted(sources))
        msg = (
            f"Measure '{measure_name}' reads columns from {joined} — several objects "
            f"whose rows this query's joins replicate. OrionBelt cannot infer which "
            f"grain to deduplicate on, and the result would be overcounted. Split the "
            f"measure so it reads from one object, or set allowFanOut: true if the "
            f"duplication is intended."
        )
        raise GrainDedupUnsupportedError(msg)

    target = next(iter(sources))

    # Everything the aggregate reads beyond its own value columns has to be
    # projected into the inner SELECT so the rendered aggregate can reference it
    # — a ``filters:`` predicate becomes CASE WHEN inside the aggregate, a
    # ``withinGroup:`` column becomes its ORDER BY. Whatever is projected joins
    # the DISTINCT, so a reference to the dedup object itself is harmless (its
    # columns are functionally determined by the key being deduplicated on)
    # while a reference to any other object splits one source row into one row
    # per distinct value of it.
    for clause, referenced in auxiliary_references(measure).items():
        outside = referenced - {target}
        # A filter is applied to the rows before they are deduplicated
        # (:func:`_cross_object_gate`), so its columns never reach the DISTINCT.
        # A sort key has to ride along into the aggregate and cannot.
        if not outside or clause == "filters":
            continue
        joined = ", ".join(f"'{s}'" for s in sorted(outside))
        msg = (
            f"Measure '{measure_name}' is sourced from '{target}', whose rows this "
            f"query's joins replicate, but its {clause} clause references {joined}. "
            f"Deduplicating on '{target}' would keep one row per distinct value of "
            f"that clause rather than one row per '{target}' row, so the result "
            f"would be overcounted. Reference '{target}' there instead, query the "
            f"measure at its own grain, or set allowFanOut: true if the duplication "
            f"is intended."
        )
        raise GrainDedupUnsupportedError(msg)

    return target


def auxiliary_references(measure: Measure) -> dict[str, set[str]]:
    """Objects a measure reads outside its own value columns, keyed by clause.

    These are the references that get projected into the deduplicating inner
    SELECT alongside the value columns, and so end up inside its DISTINCT.

    Public because ``compiler.composability`` needs the same answer: ACR must
    not advertise a measure this pass would refuse.
    """
    references: dict[str, set[str]] = {}

    filter_objects: set[str] = set()
    for item in measure.filters:
        collect_measure_filter_objects(item, filter_objects)
    if filter_objects:
        references["filters"] = filter_objects

    if measure.within_group is not None and measure.within_group.column.view:
        references["withinGroup"] = {measure.within_group.column.view}

    return references


def _identity_columns(
    object_name: str,
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> list[str]:
    """Column names identifying one row of *object_name*.

    Prefers the object's declared ``primaryKey`` columns. Falls back to the
    ``columnsTo`` of the join that reaches it — for a many-to-one those are a
    unique key of the target by definition of the declared cardinality.
    """
    obj = model.data_objects.get(object_name)
    if obj is not None:
        pk = [name for name, col in obj.columns.items() if col.primary_key]
        if pk:
            return pk

    for step in resolved.join_steps:
        if not step.reversed and step.to_object == object_name:
            return list(step.to_columns)

    return []


def dedup_identity(
    object_name: str,
    measure_names: Iterable[str],
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> list[str]:
    """The identity columns deduplicating *object_name* for *measure_names*.

    Without an identity a DISTINCT would also collapse two genuinely different
    rows that happen to share a value, turning an overcount into an
    undercount, so the query is refused rather than guessed at.
    """
    identity = _identity_columns(object_name, resolved, model)
    if identity:
        return identity
    listed = ", ".join(f"'{m}'" for m in sorted(measure_names))
    unnested = sorted({s.to_object for s in resolved.join_steps if s.nested})
    if unnested:
        # The replication came out of the FROM clause rather than a
        # join, so the usual "the join names no target columns" reads as
        # a non sequitur: there is no join. What the dedup needs is the
        # same either way - one row per row of the object being summed -
        # and here only the object itself can say which rows those are.
        objects = ", ".join(f"'{name}'" for name in unnested)
        msg = (
            f"Measure(s) {listed} are sourced from '{object_name}', whose rows "
            f"this query multiplies by unnesting {objects}: one row per array "
            f"element, so a charge carrying two labels is summed twice. "
            f"Deduplicating needs to know which rows are one row of "
            f"'{object_name}', and it declares no primaryKey. Declare one, "
            f"select the measure without the nested dimension, or set "
            f"allowFanOut: true if the duplication is intended."
        )
        raise GrainDedupUnsupportedError(msg)
    msg = (
        f"Measure(s) {listed} are sourced from '{object_name}', whose rows "
        f"this query's joins replicate, but '{object_name}' declares no "
        f"primaryKey and the join reaching it names no target columns. "
        f"Without a key OrionBelt cannot deduplicate the rows, and the "
        f"result would be overcounted. Declare primaryKey on "
        f"'{object_name}', or set allowFanOut: true if the duplication is "
        f"intended."
    )
    raise GrainDedupUnsupportedError(msg)


def _substitute_aliases(expr: Expr, by_alias: dict[str, Expr]) -> Expr:
    """Replace every bare (unqualified) ``ColumnRef`` that names a mapped alias.

    Both a metric formula and a HAVING predicate reference measures this way —
    as a table-less ``ColumnRef`` carrying the measure's name — so one
    substitution serves both.
    """
    return map_column_refs(
        expr,
        lambda ref: by_alias.get(ref.name, ref) if ref.table is None else ref,
    )


def _combine(exprs: Iterable[Expr]) -> Expr | None:
    """AND a sequence of predicates together, or ``None`` when it is empty."""
    combined: Expr | None = None
    for expr in exprs:
        combined = expr if combined is None else BinaryOp(combined, "AND", expr)
    return combined


def _reject_unmovable_having(predicates: list[Expr], data_objects: Container[str]) -> None:
    """Refuse a moved HAVING predicate that still reads a base table.

    A predicate leaving ``__ob_main`` is re-expressed against the outer query,
    where the measures and the selected dimensions survive as columns, however
    the predicate spelled them (``Tier`` or ``Customers.Tier``). Checked on the
    rewritten predicate: a reference still naming a data object's table, or a
    bare name nothing substituted, has nothing to bind to out there, so it is
    refused rather than emitted as SQL that cannot resolve.
    """
    refs: list[ColumnRef] = []
    for predicate in predicates:
        collect_column_refs(predicate, refs)
    unavailable = sorted(
        {
            f'"{ref.table}"."{ref.name}"' if ref.table is not None else ref.name
            for ref in refs
            if ref.table is None or ref.table in data_objects
        }
    )
    if not unavailable:
        return
    listed = ", ".join(unavailable)
    msg = (
        f"A HAVING filter combines {listed} with a measure that must be aggregated "
        f"over deduplicated rows. The deduplicated measure is only available "
        f"outside the grouped query, where {listed} is not. Split the filter so "
        f"the deduplicated measure is constrained on its own, move the rest to "
        f"'where', or set allowFanOut: true to aggregate the duplicated rows as-is."
    )
    raise GrainDedupUnsupportedError(msg)


def _measure_alias(column: Expr) -> str | None:
    return column.alias if isinstance(column, AliasedExpr) else None


def _counts_rows(measure: Measure | None) -> bool:
    """Whether the measure counts rows, so an empty input must read 0 rather than NULL."""
    return measure is not None and measure.aggregation.lower() in ("count", "count_distinct")


def wrap_with_grain_dedup(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    qualify_table: Callable[[DataObject], str] | None = None,
) -> Select:
    """Wrap the planner AST so one-side measures aggregate over deduplicated rows.

    With a ``childless_leg`` (see ``QueryResolver._plan_rows_without_children``)
    the parent's rows without child rows that the WHERE accepts join its
    deduplicated rows, and the query's groups are read from both.

    Returns *ast* unchanged when no measure needs dedup.
    """
    dedup_targets = resolved.dedup_targets
    leg = resolved.childless_leg
    childless: _ChildlessRows | None = None
    if leg is not None:
        _require_parent_dedup(leg, resolved, model)
        childless = _ChildlessRows(
            leg=leg, qualify_table=qualify_table or (lambda obj: obj.qualified_code)
        )
    if not dedup_targets:
        return ast

    dim_names = [d.name for d in resolved.dimensions]

    # A metric whose components the planner inlined into one expression is
    # rebuilt in the outer projection instead, over one column per component.
    # Nested derived metrics were inlined the same way, so their leaves count.
    leaf_components = {
        m.name: metric_leaf_components(m, resolved.metric_components) for m in resolved.measures
    }
    # So is any other metric reading a component this pass computes at its own
    # grain: a later wrapper leaves that component to this pass, and the
    # inlined aggregate would be read at the query's grain.
    split_metrics = {
        m.name: m
        for m in resolved.measures
        if is_split_metric(m, resolved, resolved.dedup_grain_components)
    }
    window_inputs = _window_inputs(resolved)
    # A metric over a window metric is a placeholder column until the window
    # pass computes it: its base measure is a bare name nothing here binds.
    # That pass rebuilds it from ``window_inputs``, so it is left out here.
    window_formulas = {
        m.name for m in resolved.measures if _ddm_window_components(m, resolved.metric_components)
    }

    # What each measure this pass computes aggregates, keyed by the alias it
    # lands under: the planner's own column for a selected measure (casts
    # included), the component's aggregate for one the planner inlined into a
    # metric.
    computed_here = resolved.dedup_computed
    measure_columns: dict[str, Expr] = {}
    for col in ast.columns:
        alias = _measure_alias(col)
        if alias is not None and alias in computed_here:
            measure_columns[alias] = col

    # The planner's own expression where it rewrote one: an anchored measure
    # reads its conformed subquery, not the fact table it names.
    def component_expr(name: str) -> Expr:
        component = resolved.metric_components[name]
        return resolved.projected_expressions.get(name, component.expression)

    for name in [*resolved.dedup_components, *resolved.dedup_grain_components]:
        if name not in measure_columns:
            measure_columns[name] = AliasedExpr(expr=component_expr(name), alias=name)

    # --- main CTE: the query as planned, minus the measures being deduplicated ---
    hoisted = computed_here | set(split_metrics) | window_formulas
    main_columns = [c for c in ast.columns if _measure_alias(c) not in hoisted]
    # A split metric's non-deduplicated components have to be projected in their
    # own right: the metric column that carried them is gone. One with a total
    # or a grain override is computed at that grain below instead.
    main_aliases = {_measure_alias(c) for c in main_columns}
    for metric in split_metrics.values():
        for component in leaf_components[metric.name]:
            if component.name in computed_here or component.name in main_aliases:
                continue
            main_columns.append(
                AliasedExpr(expr=component_expr(component.name), alias=component.name)
            )
            main_aliases.add(component.name)

    # A metric over a window metric is computed by the window pass, which reads
    # its inputs by name: the window's base measure and the formula's other
    # components. Those not computed here are aggregated in ``main``.
    for name in window_inputs:
        if name not in computed_here and name not in main_aliases:
            main_columns.append(AliasedExpr(expr=component_expr(name), alias=name))
            main_aliases.add(name)

    # With no dimensions and every measure deduplicated, ``main`` would project
    # nothing and degenerate to one row per base row — multiplying the single
    # scalar result. Drop it and let the dedup CTEs stand alone; each already
    # yields exactly one row at this grain.
    keep_main = bool(main_columns)
    # A group of only parents without children is in no row of ``main``, which
    # reads the child rows: the groups are then read from every CTE at the
    # query grain, and the dimensions from that list.
    split_groups = childless is not None and keep_main and bool(dim_names)
    dims_table = _GROUPS_CTE if split_groups else _MAIN_CTE

    # HAVING is evaluated inside ``main``, where a deduplicated measure does not
    # exist yet. Those predicates move to the outer query instead, where every
    # measure is one column of a CTE — the rest stay where the planner put them.
    # A predicate on a filterContext measure, or on a metric reading one, is
    # the filterContext pass's, which runs next and computes that value: here
    # it would read the query-filtered aggregate instead, and drop rows the
    # final value keeps. That pass applies it once, over its own output. A
    # predicate on a value a later wrapper windows (a rank over a deduplicated
    # measure) waits likewise, for ``PASS_HAVING_WINDOW``: here it would
    # filter the measure before the ranking.
    filter_context_owned = {
        m.name
        for m in resolved.measures
        if m.filter_context is not None
        or any(c.filter_context is not None for c in leaf_components[m.name])
    }
    windowed = windowed_aliases(resolved)
    # With groups read from every CTE, a HAVING evaluated in ``main`` removes a
    # group there only: the deduplicated CTEs bring it back with NULL values. So
    # every predicate this pass owns filters the finished rows instead.
    outer_having = [
        hf
        for hf in resolved.having_filters
        if (split_groups or hf.referenced_fields & hoisted)
        and not hf.referenced_fields & (filter_context_owned | windowed)
    ]
    main_having = ast.having
    if split_groups:
        main_having = None
    elif any(
        hf.referenced_fields & (hoisted | filter_context_owned) for hf in resolved.having_filters
    ):
        planner_measures = {m.name for m in resolved.measures}
        planner_exprs = {
            alias: col.expr
            for col in ast.columns
            if (alias := _measure_alias(col)) in planner_measures and isinstance(col, AliasedExpr)
        }
        # A predicate on a measure a *later* wrapper windows must not be
        # rebuilt into ``main`` either: the planner deliberately withheld it
        # (see ``star.py``), and ``PASS_HAVING_WINDOW`` applies it once over
        # the windowed rows. Dedup composes with totals, cumulative and direct
        # window metrics, so this path is reachable.
        deferred = hoisted | windowed | filter_context_owned
        main_having = _combine(
            _substitute_aliases(hf.expression, planner_exprs)
            for hf in resolved.having_filters
            if not hf.referenced_fields & deferred
        )

    all_ctes = list(ast.ctes)
    if keep_main:
        all_ctes.append(
            CTE(
                name=_MAIN_CTE,
                query=Select(
                    columns=main_columns,
                    from_=ast.from_,
                    joins=ast.joins,
                    where=ast.where,
                    group_by=ast.group_by,
                    having=main_having,
                    grouping=ast.grouping,
                ),
            )
        )

    # --- one dedup CTE per (source object, grain) ---
    # A measure is deduplicated at its own grain: the query's, a ``grain``
    # override's columns, or *no* grain for ``total: true`` - one row per
    # source object row across the whole query, not per group. Summing the
    # per-group values instead would double count anything belonging to
    # several groups - a product sold in two regions is legitimately in both -
    # which is exactly what a ``SUM(...) OVER ()`` over this pass's output
    # would do, and a window partitioned by an override's grain alike.
    effective_measures = model.effective_measures
    resolved_by_name = {**resolved.metric_components, **{m.name: m for m in resolved.measures}}

    def grain_of(name: str) -> tuple[str, ...]:
        measure = resolved_by_name.get(name)
        if measure is not None and measure.total:
            return ()
        if measure is not None and measure.effective_grain is not None:
            return tuple(d for d in dim_names if d in measure.effective_grain)
        return tuple(dim_names)

    # Keyed by the object deduplicated, or by "" for a component computed at
    # its grain over the plan's own rows, which need no deduplicating; and by
    # the filter a measure's rows are gated with before deduplicating, which
    # other measures of the object must not see.
    by_group: dict[tuple[str, tuple[str, ...], str], list[str]] = {}
    gates: dict[str, Expr] = {}
    for measure_name, object_name in dedup_targets.items():
        gate = _cross_object_gate(measure_name, object_name, model)
        gate_key = repr(gate) if gate is not None else ""
        if gate is not None:
            gates[gate_key] = gate
            measure_columns[measure_name] = _ungated(measure_columns[measure_name], gate)
        by_group.setdefault((object_name, grain_of(measure_name), gate_key), []).append(
            measure_name
        )
    for measure_name in resolved.dedup_grain_components:
        by_group.setdefault(("", grain_of(measure_name), ""), []).append(measure_name)

    # With parents without children, the parent's own dedup CTE reads every
    # row ``main`` does (the same FROM, joins and WHERE) plus those parents, so
    # it has every group: it drives the outer query, keeping the rows with no
    # parent (an order without a customer) as a group of their own. Not one
    # gated by a measure filter, which reads fewer rows, nor one at another
    # grain; without such a CTE the groups are listed from all of them.
    driver_key: tuple[str, tuple[str, ...], str] | None = None
    if split_groups and resolved.grouping is None and leg is not None:
        driver_key = next(
            (
                key
                for key in sorted(by_group)
                if key[0] == leg.parent and key[1] == tuple(dim_names) and not key[2]
            ),
            None,
        )
    driver_cte: str | None = None

    cte_for_measure: dict[str, str] = {}
    grain_of_cte: dict[str, tuple[str, ...]] = {}
    # CTEs carrying a row per grouping set, matched on its GROUPING() flags too.
    rolled_up: set[str] = set()
    for idx, group_key in enumerate(sorted(by_group)):
        object_name, grain, gate_key = group_key
        measure_names = by_group[group_key]
        # The rows a gated group deduplicates are the ones its filter accepts.
        rows = ast
        if gate_key:
            gate = gates[gate_key]
            rows = replace(
                ast, where=gate if ast.where is None else BinaryOp(ast.where, "AND", gate)
            )
        # Once per name: a reaggregate's first stage can list a dimension both
        # as the query's and as the metric's ``per``, and a union by name (the
        # rows without children) refuses a column named twice.
        group_dims = list({d.name: d for d in resolved.dimensions if d.name in grain}.values())
        if not object_name:
            cte_name = f"{_AT_GRAIN_CTE if grain else _AT_TOTAL_CTE}_{idx}"
            for measure_name in measure_names:
                cte_for_measure[measure_name] = cte_name
            grain_of_cte[cte_name] = grain
            all_ctes.append(
                CTE(
                    name=cte_name,
                    query=_aggregate_at_grain(
                        ast, group_dims, [measure_columns[n] for n in measure_names], model, dialect
                    ),
                )
            )
            continue
        if not grain and dim_names:
            cte_name = f"{_DEDUP_TOTAL_CTE}_{idx}"
        elif len(grain) < len(dim_names):
            cte_name = f"{_DEDUP_GRAIN_CTE}_{idx}"
        else:
            cte_name = f"{_DEDUP_CTE}_{idx}"
        for measure_name in measure_names:
            cte_for_measure[measure_name] = cte_name
        grain_of_cte[cte_name] = grain
        columns = [measure_columns[n] for n in measure_names]
        query: Select | UnionAll
        if resolved.grouping is not None and grain:
            # ROLLUP / CUBE: one deduplication per grouping set, each at that
            # set's grain, so a subtotal counts each object row once rather
            # than adding up the groups below it, which share object rows.
            query = _per_grouping_set(
                rows, object_name, measure_names, columns, resolved, model, dialect, idx
            )
            rolled_up.add(cte_name)
        else:
            query = _deduplicated_at(
                rows,
                object_name,
                measure_names,
                columns,
                group_dims,
                resolved,
                model,
                dialect,
                f"{_DEDUP_SRC}_{idx}",
                childless=childless,
                keep_keyless=group_key == driver_key,
            )
            if group_key == driver_key:
                driver_cte = cte_name
            if childless is not None:
                all_ctes.extend(childless.ctes)
                childless.ctes.clear()
        all_ctes.append(CTE(name=cte_name, query=query))

    if driver_cte is not None:
        dims_table = driver_cte

    # --- outer SELECT: main plus each dedup CTE, joined on the query grain ---
    def measure_ref(name: str) -> Expr:
        """One measure's value in the outer query, wherever it was computed."""
        ref: Expr = ColumnRef(name=name, table=cte_for_measure.get(name, _MAIN_CTE))
        # A group whose rows all miss the joined object contributes no row to the
        # dedup CTE, so the LEFT JOIN yields NULL. For a count that must read
        # zero, not "unknown" — COUNT over no rows is 0. Other aggregations keep
        # NULL, which is what SQL returns for an empty input, unless they
        # declare a default: inside the CTE it reaches no such group (a measure
        # filter applied to the rows leaves none for a group it rejects).
        if name in cte_for_measure:
            if _counts_rows(effective_measures.get(name)):
                return FunctionCall(name="COALESCE", args=[ref, Literal.number(0)])
            measure = resolved_by_name.get(name)
            if measure is not None and measure.default_value is not None:
                return measure.with_default(ref)
        return _default_for_split_group(name, ref)

    def _default_for_split_group(name: str, ref: Expr) -> Expr:
        """A group only parents without children have is no row of ``main``: a
        measure read there takes its declared default, as one over no rows does."""
        measure = resolved_by_name.get(name)
        if not split_groups or measure is None or measure.default_value is None:
            return ref
        return measure.with_default(ref)

    outer_columns: list[Expr] = []
    for col in ast.columns:
        alias = _measure_alias(col)
        if alias is None:
            outer_columns.append(col)
            continue
        if alias in window_formulas:
            continue
        if alias in split_metrics:
            outer_columns.append(
                AliasedExpr(
                    expr=metric_over_components(
                        split_metrics[alias],
                        resolved.metric_components,
                        measure_ref,
                        model,
                        dialect,
                    ),
                    alias=alias,
                )
            )
            continue
        if alias not in computed_here:
            if alias in dim_names:
                outer_columns.append(
                    AliasedExpr(expr=ColumnRef(name=alias, table=dims_table), alias=alias)
                )
                continue
            main_ref: Expr = ColumnRef(name=alias, table=_MAIN_CTE)
            # A group of only parents without children has no row in ``main``.
            if split_groups and _counts_rows(effective_measures.get(alias)):
                main_ref = FunctionCall(name="COALESCE", args=[main_ref, Literal.number(0)])
            else:
                main_ref = _default_for_split_group(alias, main_ref)
            outer_columns.append(AliasedExpr(expr=main_ref, alias=alias))
            continue
        outer_columns.append(AliasedExpr(expr=measure_ref(alias), alias=alias))

    # A split metric with a filterContext component is split again by the
    # filterContext pass, which runs next and reads its other components from
    # this query. A deduplicated one exists only as a column of a dedup CTE,
    # so each is projected here by name; re-projecting its aggregate there
    # would read tables this query no longer has. That pass returns only the
    # query's own columns.
    outer_aliases = {_measure_alias(c) for c in outer_columns}
    for metric in split_metrics.values():
        components = leaf_components[metric.name]
        if not any(c.filter_context is not None for c in components):
            continue
        for component in components:
            if component.filter_context is None and component.name not in outer_aliases:
                outer_columns.append(
                    AliasedExpr(expr=measure_ref(component.name), alias=component.name)
                )
                outer_aliases.add(component.name)

    for name in window_inputs:
        if name not in outer_aliases:
            outer_columns.append(AliasedExpr(expr=measure_ref(name), alias=name))
            outer_aliases.add(name)

    # HAVING predicates on a deduplicated measure become an outer WHERE: the
    # wrapper's own query is one row per query grain, so filtering it there is
    # exactly what HAVING would have done had the value been available.
    selected = {m.name for m in resolved.measures}
    outer_exprs = {
        alias: col.expr
        for col in outer_columns
        if (alias := _measure_alias(col)) in selected and isinstance(col, AliasedExpr)
    }
    # A selected dimension the predicate also tests is a column out here too:
    # of ``main``, or of the group list when the groups are read from it.
    to_dimensions = dimensions_to_aliases(resolved, model, dims_table)
    moved = [_substitute_aliases(to_dimensions(hf.expression), outer_exprs) for hf in outer_having]
    _reject_unmovable_having(moved, model.data_objects)
    outer_where = _combine(moved)

    # Without ``main`` the first dedup CTE becomes the FROM and the rest join to
    # it; every one yields a single row at this grain, so a CROSS JOIN is exact.
    ordered_ctes: list[str] = []
    for group_key in sorted(by_group):
        name = cte_for_measure[by_group[group_key][0]]
        if name not in ordered_ctes:
            ordered_ctes.append(name)
    outer_from = _MAIN_CTE if keep_main else ordered_ctes[0]
    joined_ctes = ordered_ctes if keep_main else ordered_ctes[1:]
    if driver_cte is not None:
        grain_of_cte[_MAIN_CTE] = tuple(dim_names)
        outer_from = driver_cte
        joined_ctes = [_MAIN_CTE, *(name for name in ordered_ctes if name != driver_cte)]
    elif split_groups:
        grain_of_cte[_MAIN_CTE] = tuple(dim_names)
        all_ctes.extend(_group_list(dim_names, [_MAIN_CTE, *ordered_ctes], grain_of_cte))
        outer_from = _GROUPS_CTE
        joined_ctes = [_MAIN_CTE, *ordered_ctes]

    outer_joins: list[Join | Unnest] = []
    for cte_name in joined_ctes:
        grain = grain_of_cte[cte_name]
        # A grand-total CTE is a single row with no grain to match on.
        if not grain:
            # Scalar grain: one row each side.
            outer_joins.append(Join(join_type=JoinType.CROSS, source=cte_name, alias=cte_name))
            continue
        on_expr: Expr | None = None
        for name in grain:
            # NULL grain values must match, or a dimension that is legitimately
            # NULL would drop its deduplicated measure.
            condition: Expr = _null_safe_eq(
                ColumnRef(name=name, table=outer_from),
                ColumnRef(name=name, table=cte_name),
            )
            if cte_name in rolled_up:
                # Matched by its GROUPING() flag, which tells a subtotal from a
                # NULL value of the dimension; and a dimension the set rolls up
                # is not compared at all, since ClickHouse shows the column's
                # default there (0, not NULL) where this CTE has NULL.
                flag = grouping_flag_alias(name)
                main_flag = ColumnRef(name=flag, table=outer_from)
                condition = BinaryOp(
                    BinaryOp(main_flag, "=", ColumnRef(name=flag, table=cte_name)),
                    "AND",
                    BinaryOp(BinaryOp(main_flag, "=", Literal.number(1)), "OR", condition),
                )
            on_expr = condition if on_expr is None else BinaryOp(on_expr, "AND", condition)
        outer_joins.append(
            Join(join_type=JoinType.LEFT, source=cte_name, alias=cte_name, on=on_expr)
        )

    # ORDER BY still holds planner-level expressions: a dimension's source
    # expression (a plain column, a time-grain call, or a whole arithmetic tree
    # for a computed column) or a measure's full aggregate. None of those survive
    # into the outer query, whose FROM is just the CTEs, so every one is matched
    # structurally against what the planner projected and repointed at its alias.
    projected_exprs: list[tuple[Expr, str]] = []
    for dim in resolved.dimensions:
        order_expr: Expr = make_dimension_expr(model, dim, dialect)
        projected_exprs.append((order_expr, dim.name))
        # The star planner rewrites a time-grained dimension to its bare alias
        # before emitting ORDER BY, so match that shape too.
        if dim.grain:
            projected_exprs.append((ColumnRef(name=dim.name), dim.name))
    projected_exprs.extend((m.expression, m.name) for m in resolved.measures)

    def order_source(name: str) -> str | None:
        # A split metric is computed in this very SELECT, so it sorts by its
        # output alias — there is no CTE holding the finished value.
        if name in split_metrics:
            return None
        if name in dim_names:
            return dims_table
        return cte_for_measure.get(name, _MAIN_CTE)

    outer_order_by = [
        OrderByItem(
            expr=_order_expr_to_outer(item.expr, projected_exprs, order_source),
            desc=item.desc,
            nulls_last=item.nulls_last,
        )
        for item in ast.order_by
    ]

    return Select(
        columns=outer_columns,
        from_=From(source=outer_from, alias=outer_from),
        joins=outer_joins,
        where=outer_where,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _window_inputs(resolved: ResolvedQuery) -> list[str]:
    """What the window pass reads by name for the query's metrics over window
    metrics: each window's base measure and the formula's other components.

    The window pass computes such a metric in its outer SELECT, over a CTE. Over
    this pass's output the fact tables are gone, so it cannot rebuild these
    aggregates; this pass projects each one under its name instead.
    """
    names: list[str] = []
    for metric in resolved.measures:
        windows = _ddm_window_components(metric, resolved.metric_components)
        if not windows:
            continue
        for component in metric_leaf_components(metric, resolved.metric_components):
            name = component.window_base_measure if component.is_window else component.name
            if name is not None and name not in names:
                names.append(name)
    return names


def _cross_object_gate(measure_name: str, object_name: str, model: SemanticModel) -> Expr | None:
    """The filter of a measure that reads beyond *object_name*, if any.

    Evaluated after deduplicating, as a ``CASE WHEN`` inside the aggregate, a
    filter on another object puts that object's columns into the DISTINCT,
    which then keeps a row per value of them rather than per object row. So
    such a filter is applied to the rows *before* they are deduplicated: a
    measure filter only decides which rows contribute (the gate gives NULL
    otherwise, and every aggregate skips NULL), so dropping those rows first is
    the same answer. A filter on the object's own columns stays where it is.
    """
    measure = model.effective_measures.get(measure_name)
    if measure is None or not measure.filters:
        return None
    objects: set[str] = set()
    for item in measure.filters:
        collect_measure_filter_objects(item, objects)
    if not objects - {object_name}:
        return None
    errors: list[SemanticError] = []
    gate = build_measure_filter_condition(measure.filters, model, errors)
    if gate is None:
        raise ResolutionError(errors)
    return gate


def _ungated(column: Expr, gate: Expr) -> Expr:
    """*column* with the ``CASE WHEN <gate> THEN arg END`` around each aggregate
    argument taken off, the gate being applied to the rows instead."""

    def strip(node: Expr) -> Expr | None:
        if (
            isinstance(node, CaseExpr)
            and node.else_clause is None
            and len(node.when_clauses) == 1
            and node.when_clauses[0][0] == gate
        ):
            return node.when_clauses[0][1]
        return None

    return map_nodes(column, strip)


def _deduplicated_at(
    ast: Select,
    object_name: str,
    measure_names: list[str],
    measure_columns: list[Expr],
    group_dims: list[ResolvedDimension],
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    src_alias: str,
    *,
    childless: _ChildlessRows | None = None,
    keep_keyless: bool = False,
) -> Select:
    """*measure_columns* aggregated over the rows of *object_name*, each once
    per group of *group_dims*: the query's rows reduced by a DISTINCT over the
    grain, the object's identity and every column the measures read.

    With *childless* for this object, its rows without child rows that the
    query's WHERE accepts are added beside them (:func:`_rows_without_children`).

    With *keep_keyless*, a row with no *object_name* row (an order without a
    customer) stays, so its group does too: this CTE then has every group of
    the query and drives it. Its values are NULL, which every aggregate skips;
    a row count counts the key instead of the row.
    """
    inner_columns: list[Expr] = [
        AliasedExpr(expr=make_dimension_expr(model, dim, dialect), alias=dim.name)
        for dim in group_dims
    ]
    identity = dedup_identity(object_name, measure_names, resolved, model)
    for key_idx, key_column in enumerate(identity):
        inner_columns.append(
            AliasedExpr(
                expr=make_column_expr(model, object_name, key_column),
                alias=f"{_KEY_PREFIX}{key_idx}",
            )
        )

    refs: list[ColumnRef] = []
    for column in measure_columns:
        collect_column_refs(column, refs)
    ref_map: dict[tuple[str, str | None], ColumnRef] = {}
    for col_idx, ref in enumerate(refs):
        projected = f"{_COL_PREFIX}{col_idx}"
        inner_columns.append(AliasedExpr(expr=ref, alias=projected))
        ref_map[(ref.name, ref.table)] = ColumnRef(name=projected, table=src_alias)

    deduplicated = Select(
        columns=inner_columns,
        from_=ast.from_,
        joins=ast.joins,
        where=ast.where,
        distinct=True,
    )
    source: str | Select = deduplicated
    if childless is not None and object_name == childless.leg.parent:
        rows_name = f"{src_alias}_rows"
        without = _rows_without_children(
            childless, ast, object_name, identity, refs, group_dims, resolved, model, dialect
        )
        childless.ctes.append(CTE(name=rows_name, query=UnionAll(queries=[deduplicated, without])))
        source = rows_name

    # Outer: re-aggregate the measures against the deduplicated rows. The
    # measure expressions keep their casts and shape; only their leaf column
    # refs are repointed at the subquery projection.
    cte_columns: list[Expr] = [
        AliasedExpr(expr=ColumnRef(name=dim.name, table=src_alias), alias=dim.name)
        for dim in group_dims
    ]
    cte_columns.extend(rewrite_column_refs(column, ref_map) for column in measure_columns)

    # A base row with no match on the joined object still yields a row, with
    # the whole object NULL. It is not one of the object's rows, so drop it:
    # otherwise a grain-anchored COUNT would count a phantom. SUM and the
    # rest already ignore NULL inputs, so this changes nothing for them.
    key_present: Expr | None = None
    for key_idx in range(len(identity)):
        not_null = IsNull(
            expr=ColumnRef(name=f"{_KEY_PREFIX}{key_idx}", table=src_alias), negated=True
        )
        key_present = not_null if key_present is None else BinaryOp(key_present, "AND", not_null)
    if keep_keyless and key_present is not None:
        present = key_present

        def gated(node: Expr) -> Expr | None:
            # A value is read only from a row the object has: ClickHouse fills an
            # unmatched non-Nullable column with its default (0, ''), not NULL.
            if (
                isinstance(node, ColumnRef)
                and node.table == src_alias
                and node.name.startswith(_COL_PREFIX)
            ):
                return CaseExpr(when_clauses=[(present, node)])
            return _count_present(node, present)

        cte_columns = [
            cte_columns[i] if i < len(group_dims) else map_nodes(column, gated)
            for i, column in enumerate(cte_columns)
        ]
        key_present = None

    return Select(
        columns=cte_columns,
        from_=From(source=source, alias=src_alias),
        where=key_present,
        group_by=[ColumnRef(name=dim.name, table=src_alias) for dim in group_dims],
    )


def _count_present(node: Expr, present: Expr) -> Expr | None:
    """``COUNT(*)`` / ``COUNT(1)`` counting only the rows where *present* holds.

    ``COUNT(column)`` and every other aggregate already skip the NULL values
    of a row with no object row; a count of rows does not."""
    if (
        isinstance(node, FunctionCall)
        and node.name.upper() == "COUNT"
        and not node.distinct
        and len(node.args) == 1
        and isinstance(node.args[0], Star | Literal)
    ):
        return replace(node, args=[CaseExpr(when_clauses=[(present, Literal.number(1))])])
    return None


@dataclass
class _ChildlessRows:
    """What :func:`_rows_without_children` needs beyond one dedup group's own."""

    leg: ChildlessLeg
    qualify_table: Callable[[DataObject], str]
    #: CTEs the rows are emitted as, added to the query ahead of the dedup CTEs.
    ctes: list[CTE] = field(default_factory=list)


def _require_parent_dedup(leg: ChildlessLeg, resolved: ResolvedQuery, model: SemanticModel) -> None:
    """Every measure on the parent is deduplicated, the only path its rows
    without children reach. One that is not (``allowFanOut``, a delegated
    aggregation) refuses the query instead."""
    effective = model.effective_measures
    # A filterContext measure is computed by a scan planned in its own right,
    # which applies its own filters (and its own leg) or none.
    names = {
        m.name for m in resolved.measures if not m.component_measures and m.filter_context is None
    } | {
        c.name
        for m in resolved.measures
        if m.component_measures
        for c in metric_leaf_components(m, resolved.metric_components)
        if not c.component_measures and c.filter_context is None
    }
    missed = sorted(
        name
        for name in names
        if (measure := effective.get(name)) is not None
        and measure_source_objects(measure) == {leg.parent}
        and name not in resolved.dedup_targets
    )
    if missed:
        listed = ", ".join(f"'{name}'" for name in missed)
        raise _unsplittable(leg, f"{listed} cannot be deduplicated (allowFanOut)")


def _group_list(
    dim_names: list[str], sources: list[str], grain_of_cte: dict[str, tuple[str, ...]]
) -> list[CTE]:
    """The query's groups, read from every CTE at its grain: ``main`` and the
    deduplicated ones, which have the groups of parents without children too."""
    branches = [
        Select(
            columns=[
                AliasedExpr(expr=ColumnRef(name=name, table=source), alias=name)
                for name in dim_names
            ],
            from_=From(source=source, alias=source),
        )
        for source in sources
        if grain_of_cte.get(source) == tuple(dim_names)
    ]
    listed = Select(
        columns=[AliasedExpr(expr=ColumnRef(name=name), alias=name) for name in dim_names],
        from_=From(source=_GROUPS_ALL_CTE, alias=_GROUPS_ALL_CTE),
        distinct=True,
    )
    return [
        CTE(name=_GROUPS_ALL_CTE, query=UnionAll(queries=branches)),
        CTE(name=_GROUPS_CTE, query=listed),
    ]


def _rows_without_children(
    childless: _ChildlessRows,
    ast: Select,
    object_name: str,
    identity: list[str],
    refs: list[ColumnRef],
    group_dims: list[ResolvedDimension],
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
) -> Select:
    """The rows of *object_name* with no child row that the WHERE accepts.

    Projected like the deduplicated rows they are unioned with: the grain, the
    object's identity, the columns the measures read. A dimension the parent
    cannot reach (one on the child, an order status) is NULL, as a ``LEFT
    JOIN`` would leave it. The WHERE is the query's own with every column of
    the child tables NULL, typed so a function over one still resolves, beside
    ``NOT EXISTS`` a child row. The parent's own joins are many-to-one, so each
    parent row is one row here: nothing to deduplicate, and no child rows to
    multiply anything.
    """
    leg = childless.leg
    graph = JoinGraph.of(model, use_path_names=resolved.use_path_names or None)
    # What the query joins through the parent: an object it reaches through
    # the child keeps that binding, and is NULL here like the child itself.
    reachable = joined_beneath(resolved.join_steps, leg.parent)

    columns: list[Expr] = []
    needed: set[str] = set()
    for dim in group_dims:
        # The dimension as it reads for a parent without children: every column
        # of an object the parent cannot reach NULL, as a LEFT JOIN leaves the
        # whole child side, and a computed one over them evaluated as such
        # (``COALESCE(product, 'missing')`` is 'missing'). A ``via`` role the
        # parent cannot take is NULL outright.
        value = map_nodes(
            make_dimension_expr(model, dim, dialect),
            lambda node: _as_childless(node, reachable, model),
        )
        tables: set[str] = set()
        collect_referenced_tables(value, tables)
        if tables <= reachable and (dim.via is None or dim.via in reachable):
            needed |= tables
        else:
            value = _dimension_null(model, dim)
        columns.append(AliasedExpr(expr=value, alias=dim.name))
    columns.extend(
        AliasedExpr(expr=make_column_expr(model, object_name, key), alias=f"{_KEY_PREFIX}{i}")
        for i, key in enumerate(identity)
    )
    for col_idx, ref in enumerate(refs):
        columns.append(AliasedExpr(expr=ref, alias=f"{_COL_PREFIX}{col_idx}"))
        if ref.table is not None:
            needed.add(ref.table)

    accepts = (
        map_nodes(ast.where, lambda node: _as_childless(node, reachable, model))
        if ast.where is not None
        else None
    )
    if accepts is not None:
        collect_referenced_tables(accepts, needed)
    unreachable = needed - reachable
    if unreachable:
        raise _unsplittable(leg, f"'{leg.parent}' cannot reach {', '.join(sorted(unreachable))}")

    errors: list[SemanticError] = []
    no_child = build_exists_filter_expr(
        QueryFilter(
            field=leg.parent,
            op=FilterOperator.NONEXISTS,
            subquery=Subquery(data_object=leg.child),
        ),
        model,
        leg.parent,
        childless.qualify_table,
        errors,
        use_path_names=resolved.use_path_names or None,
    )
    if no_child is None:
        raise ResolutionError(errors)
    where: Expr = no_child if accepts is None else BinaryOp(no_child, "AND", accepts)

    joins: list[Join | Unnest] = []
    joined = {leg.parent}
    targets = needed - joined
    if targets:
        for step in graph.find_join_path(
            {leg.parent}, targets | joined, via_constraints=resolved.via_constraints or None
        ):
            target = model.data_objects.get(step.to_object)
            if target is None or step.to_object in joined:
                continue
            joins.append(
                Join(
                    join_type=step.join_type,
                    source=childless.qualify_table(target),
                    alias=step.to_object,
                    on=graph.build_join_condition(step),
                )
            )
            joined.add(step.to_object)

    parent = model.data_objects[leg.parent]
    return Select(
        columns=columns,
        from_=From(source=childless.qualify_table(parent), alias=leg.parent),
        joins=joins,
        where=where,
        distinct=True,
    )


def _as_childless(node: Expr, reachable: set[str], model: SemanticModel) -> Expr | None:
    """*node* as it reads for a parent row with no child rows: a column of an
    object the parent cannot reach NULL, an ``EXISTS`` correlated to one
    finding nothing."""
    if isinstance(node, ColumnRef) and node.table is not None and node.table not in reachable:
        return _typed_null(model, node)
    if isinstance(node, Exists) and correlated_tables(node.subquery) - reachable:
        # 1 = 0 / 1 = 1: a boolean literal every dialect accepts.
        return BinaryOp(Literal.number(1), "=", Literal.number(1 if node.negated else 0))
    return None


def _dimension_null(model: SemanticModel, dim: ResolvedDimension) -> Expr:
    """A dimension's NULL in the second branch of a positional union.

    BigQuery types a bare NULL as INT64 and will not unify it with the STRING
    of the first branch, so a plain column's NULL carries that column's
    declared type, which is what the first branch reads. A time grain or a
    computed column renders a type of its own per engine; there a bare NULL
    takes the first branch's type everywhere else (see ``_per_grouping_set``).
    """
    obj = model.data_objects.get(dim.object_name)
    column = obj.columns.get(dim.column_name) if obj is not None else None
    if dim.grain is not None or column is None or column.expression or not column.abstract_type:
        return Literal.null()
    return Cast(expr=Literal.null(), type_name=column.abstract_type.value)


def _typed_null(model: SemanticModel, ref: ColumnRef) -> Expr:
    """NULL with the type of the column *ref* reads, when the model declares one."""
    obj = model.data_objects.get(ref.table or "")
    column = (
        next((c for c in obj.columns.values() if c.code == ref.name), None)
        if obj is not None
        else None
    )
    if column is None or column.abstract_type is None:
        return Literal.null()
    return Cast(expr=Literal.null(), type_name=column.abstract_type.value)


def _unsplittable(leg: ChildlessLeg, reason: str) -> ResolutionError:
    """The rows without children cannot be added, so the WHERE is refused."""
    objects = ", ".join(f"'{name}'" for name in sorted(leg.children))
    return ResolutionError(
        [
            SemanticError(
                code="FILTER_ADMITS_ROWS_WITHOUT",
                message=(
                    f"The WHERE can hold for a '{leg.parent}' row with no {objects} rows, "
                    f"and such rows cannot be added to this query: {reason}. Answered "
                    f"from the {objects} rows alone, they would be left out without a word."
                ),
                path="where",
                hint=(
                    f"Write the condition on {objects} as 'exists' / 'nonexists', which "
                    f"tests for a matching row and keeps the ones without any."
                ),
            )
        ]
    )


def _grouping_sets(
    dims: list[ResolvedDimension], grouping: Grouping
) -> list[list[ResolvedDimension]]:
    """The grouping sets ROLLUP or CUBE expands *dims* into, finest first."""
    if grouping is Grouping.ROLLUP:
        return [dims[:size] for size in range(len(dims), -1, -1)]
    return [
        [dims[i] for i in kept]
        for size in range(len(dims), -1, -1)
        for kept in combinations(range(len(dims)), size)
    ]


def _per_grouping_set(
    ast: Select,
    object_name: str,
    measure_names: list[str],
    measure_columns: list[Expr],
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
    idx: int,
) -> UnionAll:
    """One deduplicated aggregate per grouping set of the query's ROLLUP or
    CUBE, stacked: every dimension (NULL where the set rolls it up), its
    ``GROUPING()`` flag as a constant, and the measures."""
    assert resolved.grouping is not None
    branches: list[Select] = []
    for set_idx, kept in enumerate(_grouping_sets(resolved.dimensions, resolved.grouping)):
        src_alias = f"{_DEDUP_SRC}_{idx}_{set_idx}"
        inner = _deduplicated_at(
            ast,
            object_name,
            measure_names,
            measure_columns,
            kept,
            resolved,
            model,
            dialect,
            f"{src_alias}_rows",
        )
        kept_names = {dim.name for dim in kept}
        columns: list[Expr] = []
        for dim in resolved.dimensions:
            # An untyped NULL takes its type from the finest set's branch,
            # which comes first and carries the dimension's own expression. A
            # declared resultType can differ from it (it defaults to string),
            # and a cast to it fails the union on PostgreSQL.
            value: Expr = (
                ColumnRef(name=dim.name, table=src_alias)
                if dim.name in kept_names
                else Literal.null()
            )
            columns.append(AliasedExpr(expr=value, alias=dim.name))
        columns.extend(
            AliasedExpr(
                expr=Literal.number(0 if dim.name in kept_names else 1),
                alias=grouping_flag_alias(dim.name),
            )
            for dim in resolved.dimensions
        )
        columns.extend(
            AliasedExpr(expr=ColumnRef(name=name, table=src_alias), alias=name)
            for name in measure_names
        )
        branches.append(Select(columns=columns, from_=From(source=inner, alias=src_alias)))
    return UnionAll(queries=branches)


def _aggregate_at_grain(
    ast: Select,
    group_dims: list[ResolvedDimension],
    measure_columns: list[Expr],
    model: SemanticModel,
    dialect: Dialect,
) -> Select:
    """The planned rows, aggregated at *group_dims* rather than the query's grain.

    For a component the plan's joins do not replicate, so its own rows need no
    deduplicating: the query's FROM, joins and WHERE, grouped by the grain's
    dimensions, or by nothing for a total.
    """
    dim_exprs = [make_dimension_expr(model, dim, dialect) for dim in group_dims]
    return Select(
        columns=[
            *(
                AliasedExpr(expr=expr, alias=dim.name)
                for expr, dim in zip(dim_exprs, group_dims, strict=True)
            ),
            *measure_columns,
        ],
        from_=ast.from_,
        joins=ast.joins,
        where=ast.where,
        group_by=dim_exprs,
    )


def _null_safe_eq(left: Expr, right: Expr) -> Expr:
    """``left = right`` that also matches when both sides are NULL.

    Rendered as ``a = b OR (a IS NULL AND b IS NULL)`` rather than a dialect
    ``IS NOT DISTINCT FROM`` / ``<=>`` so it stays portable across all eight
    dialects with no codegen change.
    """
    return BinaryOp(
        left=BinaryOp(left, "=", right),
        op="OR",
        right=BinaryOp(IsNull(expr=left), "AND", IsNull(expr=right)),
    )


def _order_expr_to_outer(
    expr: Expr,
    projected: list[tuple[Expr, str]],
    source_of: Callable[[str], str | None],
) -> Expr:
    """Repoint one ORDER BY expression at the wrapper's CTE aliases.

    *projected* pairs each expression the planner emitted with the alias it was
    emitted under. Matching is structural (AST nodes are frozen dataclasses, so
    ``==`` compares by value), which covers a computed dimension's whole
    arithmetic tree as readily as a plain column reference.

    A deduplicated measure sorts against the CTE that computes it; everything
    else against ``main``, which carries the full grain. *source_of* answers
    which, and returns ``None`` for a metric this query rebuilds in its own
    projection — that one sorts by its output alias.
    """
    # An ordering key that already names an output alias is kept to it: a
    # derived metric over another (``{[Average Order]}``) has that bare alias as
    # its expression, and matching it would sort by the wrong column.
    if (
        isinstance(expr, ColumnRef)
        and expr.table is None
        and any(expr.name == name for _, name in projected)
    ):
        return ColumnRef(name=expr.name, table=source_of(expr.name))

    for candidate, name in projected:
        if expr == candidate:
            return ColumnRef(name=name, table=source_of(name))

    # Resolution rejects an ORDER BY that is not in the SELECT, so anything left
    # is already an alias reference.
    if isinstance(expr, ColumnRef) and expr.table is None:
        return ColumnRef(name=expr.name, table=source_of(expr.name))

    return expr


def mixed_grain_measures(resolved: ResolvedQuery, model: SemanticModel) -> dict[str, list[str]]:
    """Measures reading a replicated object *and* a base-grain one, unsafely aggregated.

    :func:`detect_dedup_measures` deliberately leaves these alone: evaluated per
    base row they are correct for the pattern they were designed for, an
    extended price like ``{[Sales].[Quantity]} * {[Products].[List Price]}``,
    where the replicated column is a per-unit rate and multiplying it by a
    base-grain quantity is the whole point.

    The same shape is wrong when the replicated column carries the replicated
    row's own magnitude - ``{[Sales].[Amount]} * {[Returns].[Quantity]}`` counts
    a sale's amount once per return of it. Nothing in the declarations tells the
    two apart, which is why this warns rather than refuses.

    Only multiplicity-sensitive aggregations qualify. ``MIN`` / ``MAX`` /
    ``COUNT DISTINCT`` read the same answer off duplicated rows, so they are
    silent. ``AVG`` is *not* among them: an average over replicated rows is
    weighted by the replication, so it is as exposed as ``SUM``.

    Returns each measure mapped to the replicated objects it reads.
    """
    if not resolved.join_steps:
        return {}
    replicated = replicated_objects(resolved)
    if not replicated:
        return {}

    effective = model.effective_measures
    flagged: dict[str, list[str]] = {}
    # Metric components count: a metric inlines its components, so selecting
    # ``{[Mixed]} + 1`` compiles the same duplicated expression as selecting
    # ``Mixed`` and deserves the same warning.
    seen: set[str] = set()
    for resolved_measure in (*resolved.measures, *resolved.metric_components.values()):
        if resolved_measure.name in seen:
            continue
        seen.add(resolved_measure.name)
        measure = effective.get(resolved_measure.name)
        if measure is None or measure.allow_fan_out:
            continue
        aggregation = measure.aggregation.lower()
        if aggregation in MULTIPLICITY_SAFE_AGGREGATIONS or aggregation == _DELEGATED_AGGREGATION:
            continue
        objects = measure.source_objects
        hit = objects & replicated
        # Wholly-replicated measures are the dedup pass's job, not this one.
        if hit and objects - replicated:
            flagged[resolved_measure.name] = sorted(hit)
    return flagged


def mixed_grain_warning(flagged: dict[str, list[str]]) -> SemanticError:
    """Warn that a measure reads a replicated object's rows more than once."""
    listed = ", ".join(f"'{m}'" for m in sorted(flagged))
    objects = sorted({obj for objs in flagged.values() for obj in objs})
    return warning(
        code=WarningCode.FAN_TRAP_RISK,
        message=(
            f"Measure(s) {listed} read both a base-grain column and one from "
            f"{', '.join(repr(o) for o in objects)}, whose rows this query's joins "
            f"replicate. The expression is evaluated once per base row, so the "
            f"replicated column contributes once per duplicate. That is intended for a "
            f"per-unit rate (quantity x list price) and wrong for a column carrying the "
            f"replicated row's own magnitude."
        ),
        hint=(
            "Check the replicated column is a rate rather than a total. Set "
            "allowFanOut: true on the query or the measure once it is understood."
        ),
        context={"measures": sorted(flagged), "replicated": objects},
    )


def dedup_warning(dedup_measures: dict[str, str]) -> SemanticError:
    """Warn that deduplicated groups overlap, so the column will not sum to a total."""
    listed = ", ".join(f"'{m}'" for m in sorted(dedup_measures))
    return warning(
        code=WarningCode.FAN_TRAP_RISK,
        message=(
            f"Measure(s) {listed} are sourced from an object whose rows this query's "
            f"joins replicate. Each was aggregated over rows deduplicated on that "
            f"object's key, so per-group values are correct - but one row can belong "
            f"to several groups, so the values do not add up to that object's grand "
            f"total."
        ),
        hint=(
            "Query the measure at its own grain for a total that adds up, or set "
            "allowFanOut: true to aggregate the duplicated rows as-is."
        ),
        context={
            "measures": sorted(dedup_measures),
            "dedupOn": sorted(set(dedup_measures.values())),
        },
    )
