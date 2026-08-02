"""First-class compiler passes for the aggregate-mode wrapper stage.

The compilation pipeline applies a fixed sequence of AST wrappers after
planning (filter context, period-over-period, totals, cumulative, window)
plus a final ``having`` projection cleanup. Historically this sequence and
its feature-compatibility rules lived as inline ``if`` blocks inside
``CompilationPipeline.compile()``.

This module makes that composition explicit:

* :class:`CompilerPass` describes one transformation (its name, an
  ``applies`` predicate, the ``run`` callable, and the metadata needed to
  reason about ordering and incompatibilities).
* :func:`build_default_passes` declares the pass order **once**.
* :func:`evaluate_compatibility` centralizes every cross-feature
  compatibility rule in a single function that returns structured
  warnings plus the set of passes to suppress.
* :func:`apply_aggregate_passes` runs the passes against a
  :class:`CompileContext`.

Behaviour is intentionally identical to the previous inline orchestration:
the per-feature predicates mirror the wrappers' own internal guards, the
declared order matches the previous call order, and the compatibility
warnings reproduce the original messages, hints, context, and ordering so
generated SQL and explain output stay byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from orionbelt.ast.nodes import AliasedExpr, Select
from orionbelt.compiler.cumulative_wrap import wrap_with_cumulative
from orionbelt.compiler.filter_wrap import wrap_with_filter_context
from orionbelt.compiler.grain_dedup import (
    GrainDedupUnsupportedError,
    dedup_warning,
    mixed_grain_measures,
    mixed_grain_warning,
    wrap_with_grain_dedup,
)
from orionbelt.compiler.metric_expansion import metric_leaf_components
from orionbelt.compiler.pop_wrap import wrap_with_pop
from orionbelt.compiler.resolution import ResolutionError, ResolvedQuery
from orionbelt.compiler.total_wrap import wrap_with_totals
from orionbelt.compiler.window_wrap import (
    _ddm_window_components,
    window_pass_applies,
    wrap_with_window,
)
from orionbelt.dialect.base import Dialect
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import DataObject, SemanticModel
from orionbelt.models.warnings import WarningCode, warning

# Canonical pass names. Used as identifiers in ordering, compatibility
# metadata, and tests — keep them stable.
PASS_GRAIN_DEDUP = "grain_dedup"
PASS_FILTER_CONTEXT = "filter_context"
PASS_PERIOD_OVER_PERIOD = "period_over_period"
PASS_TOTALS = "totals"
PASS_CUMULATIVE = "cumulative"
PASS_WINDOW = "window"
PASS_HAVING_CLEANUP = "having_projection_cleanup"


@dataclass(frozen=True)
class CompileContext:
    """Shared inputs every aggregate-mode pass needs.

    Bundles the resolution result and the dialect/model context so passes
    share one signature, ``run(ast, ctx) -> Select``.
    """

    resolved: ResolvedQuery
    model: SemanticModel
    dialect: Dialect
    qualify_table: Callable[[DataObject], str]


@dataclass(frozen=True)
class CompilerPass:
    """A single AST transformation in the aggregate-mode stage."""

    name: str
    applies: Callable[[ResolvedQuery], bool]
    run: Callable[[Select, CompileContext], Select]
    requires: frozenset[str] = frozenset()
    produces: frozenset[str] = frozenset()
    incompatible_with: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CompatibilityResult:
    """Outcome of evaluating cross-feature compatibility rules."""

    warnings: list[SemanticError] = field(default_factory=list)
    suppressed: frozenset[str] = frozenset()


def _drop_having_only_projection(ast: Select, ctx: CompileContext) -> Select:
    """Strip auto-included HAVING-only measures from the outermost SELECT.

    The resolver auto-includes any measure referenced by HAVING but not
    listed in ``select.measures`` (so the SQL stays valid). The planner /
    aggregation wrappers then project that measure in their outer SELECT,
    which would leak it into the user's output as an extra column.

    HAVING itself emits the aggregate inline (not via the alias), so
    dropping the having-only column from the outermost SELECT keeps the
    HAVING reference valid. Inner CTEs / leg projections are untouched:
    those still need the column for aggregation.
    """
    resolved = ctx.resolved
    if not resolved.having_only_measures:
        return ast
    kept_columns = [
        col
        for col in ast.columns
        if not (isinstance(col, AliasedExpr) and col.alias in resolved.having_only_measures)
    ]
    if len(kept_columns) == len(ast.columns):
        return ast
    return replace(ast, columns=kept_columns)


def build_default_passes() -> tuple[CompilerPass, ...]:
    """Declare the aggregate-mode pass order once.

    The order is load-bearing: filter context and PoP rewrite the base
    structure that totals/cumulative/window then wrap, and the window pass
    runs after cumulative so window metrics can compose over cumulative
    outputs. The ``having`` cleanup runs last so it sees the final
    projection.
    """
    return (
        CompilerPass(
            name=PASS_GRAIN_DEDUP,
            applies=lambda r: bool(r.dedup_targets),
            run=lambda ast, ctx: wrap_with_grain_dedup(ast, ctx.resolved, ctx.model, ctx.dialect),
            # Runs first: it rewrites the base projection into CTEs that every
            # later wrapper then wraps. The wrappers below restructure that same
            # projection, so combining them is rejected in
            # ``evaluate_compatibility`` rather than composed.
            incompatible_with=frozenset(
                {
                    PASS_FILTER_CONTEXT,
                    PASS_PERIOD_OVER_PERIOD,
                    PASS_TOTALS,
                    PASS_CUMULATIVE,
                    PASS_WINDOW,
                }
            ),
        ),
        CompilerPass(
            name=PASS_FILTER_CONTEXT,
            applies=lambda r: r.has_filter_context,
            run=lambda ast, ctx: wrap_with_filter_context(
                ast, ctx.resolved, ctx.model, ctx.dialect, ctx.qualify_table
            ),
        ),
        CompilerPass(
            name=PASS_PERIOD_OVER_PERIOD,
            applies=lambda r: r.has_pop,
            run=lambda ast, ctx: wrap_with_pop(
                ast, ctx.resolved, ctx.model, ctx.dialect, ctx.qualify_table
            ),
        ),
        CompilerPass(
            name=PASS_TOTALS,
            applies=lambda r: r.has_totals,
            run=lambda ast, ctx: wrap_with_totals(ast, ctx.resolved),
            # Totals rewrites the AST structure that PoP / cumulative
            # wrappers depend on, producing invalid SQL when combined.
            incompatible_with=frozenset({PASS_PERIOD_OVER_PERIOD, PASS_CUMULATIVE}),
        ),
        CompilerPass(
            name=PASS_CUMULATIVE,
            applies=lambda r: r.has_cumulative,
            run=lambda ast, ctx: wrap_with_cumulative(
                ast, ctx.resolved, model=ctx.model, dialect=ctx.dialect
            ),
        ),
        CompilerPass(
            name=PASS_WINDOW,
            # Window also runs when a derived metric transitively references a
            # window metric, so the predicate is the wrapper's own guard, not
            # just ``has_window``.
            applies=window_pass_applies,
            run=lambda ast, ctx: wrap_with_window(
                ast, ctx.resolved, model=ctx.model, dialect=ctx.dialect
            ),
        ),
        CompilerPass(
            name=PASS_HAVING_CLEANUP,
            applies=lambda r: bool(r.having_only_measures),
            run=_drop_having_only_projection,
        ),
    )


def _conflicts_with_dedup(pass_name: str, resolved: ResolvedQuery) -> bool:
    """Whether *pass_name* genuinely conflicts with the grain-dedup rewrite.

    The wrappers run *after* dedup, on the CTEs it produced. Whether that is a
    conflict depends on which measure carries the feature, not on whether the
    query contains one anywhere.

    ``totals`` composes, because it selects the base measure *by alias* from the
    CTE beneath it: it wraps the dedup output in a ``base`` CTE and adds
    ``AGG(x) OVER ()`` around a column the dedup CTEs already finished. So a
    total on a base-grain measure never touches the deduplicated one. Only a
    total on a measure that is *itself* deduplicated conflicts — that value
    lives in a dedup CTE the totals wrapper does not reach into.

    The rest stay blocked whenever they apply at all. Each was checked by
    letting it through and executing the result:

    * ``cumulative`` and ``window`` re-project the measure's *raw aggregate*
      rather than selecting it by alias, emitting ``SUM("Sales"."quantity")``
      into a CTE whose FROM is only ``main``/``dedup_0`` —
      ``Referenced table "Sales" not found``. This is the dividing line against
      ``totals``: alias reference composes, expression re-projection does not.
    * ``filter_context`` emits its own CTE named ``main``, colliding with the
      one dedup emits — ``Duplicate CTE name "main"``.
    * ``period_over_period`` rebuilds the FROM from a date spine and re-joins
      the tables the dedup CTEs already joined —
      ``Ambiguous reference to table ... duplicate alias``.

    Cumulative and window could be made to compose by selecting from the dedup
    output by alias, the same repointing ``grain_dedup`` already does for
    ORDER BY. That is a change inside those wrappers, not a predicate here.
    """
    if pass_name == PASS_CUMULATIVE:
        return False
    if pass_name == PASS_WINDOW:
        # A derived metric over window metrics needs several base measures
        # out of one column, which re-aliasing cannot supply.
        return any(_ddm_window_components(m, resolved.metric_components) for m in resolved.measures)
    if pass_name != PASS_TOTALS:
        return True
    # ``total`` on a deduplicated measure is handled by the dedup pass itself,
    # which computes it in a CTE deduplicated at no grain. A ``grain`` override
    # is not: its target grain would need its own dedup CTE, which is not built
    # yet, so it still conflicts.
    if any(
        measure.grain_override is not None
        for measure in resolved.measures
        if measure.name in resolved.dedup_measures
    ):
        return True
    # Once a metric is split across dedup CTEs, a ``total`` or ``grain`` on *any*
    # of its components conflicts — not just on the deduplicated ones. The totals
    # wrapper decomposes such a metric back into its components and re-projects
    # each one's raw aggregate into a base CTE whose FROM is the dedup output,
    # where the fact tables are gone: ``SUM("Products"."stock_on_hand")`` over
    # ``__ob_main`` binds to nothing.
    for metric in resolved.measures:
        components = metric_leaf_components(metric, resolved.metric_components)
        if not any(comp.name in resolved.dedup_components for comp in components):
            continue
        if any(comp.total or comp.grain_override is not None for comp in components):
            return True
    return False


def evaluate_compatibility(
    resolved: ResolvedQuery, passes: tuple[CompilerPass, ...]
) -> CompatibilityResult:
    """Evaluate every cross-feature compatibility rule in one place.

    Returns the warnings to record (in a stable order) and the set of pass
    names to suppress. The warning messages, hints, context payloads, and
    their relative order reproduce the previous inline behaviour exactly.
    """
    warnings: list[SemanticError] = []
    suppressed: set[str] = set()

    # Rule 1 (advisory only): ROLLUP/CUBE wraps the base CTE inside the
    # total/PoP/cumulative/window wrappers, but the outer wrapper SELECTs by
    # dim/measure name, so GROUPING() flag columns won't survive. Warn but
    # still run the wrappers. The window check uses the pass predicate, not
    # ``has_window``, so a derived metric that transitively references a
    # window metric (which still runs the window pass) also triggers the
    # advisory.
    if resolved.grouping is not None and (
        resolved.has_totals
        or resolved.has_pop
        or resolved.has_cumulative
        or window_pass_applies(resolved)
    ):
        warnings.append(
            warning(
                code=WarningCode.INCOMPATIBLE_COMBINATION,
                message=(
                    "ROLLUP/CUBE combined with total / period-over-period / "
                    "cumulative measures — GROUPING() flag columns may not "
                    "appear in the final projection. Subtotal rows are still "
                    "produced, but callers cannot distinguish them from "
                    "detail rows whose rolled-up dim is legitimately NULL."
                ),
                hint=(
                    "Avoid combining `grouping: rollup|cube` with "
                    "`total: true`, period-over-period metrics, or cumulative "
                    "metrics in the same query."
                ),
                context={
                    "grouping": resolved.grouping.value,
                    "has_totals": resolved.has_totals,
                    "has_pop": resolved.has_pop,
                    "has_cumulative": resolved.has_cumulative,
                },
            )
        )

    # Rule 2 (suppressing): totals combined with PoP or cumulative produces
    # invalid SQL, so the totals pass is skipped and a warning recorded.
    by_name = {p.name: p for p in passes}
    totals = by_name.get(PASS_TOTALS)
    if totals is not None and totals.applies(resolved):
        conflicting = [
            name
            for name in totals.incompatible_with
            if (p := by_name.get(name)) is not None and p.applies(resolved)
        ]
        if conflicting:
            suppressed.add(PASS_TOTALS)
            warnings.append(
                warning(
                    code=WarningCode.INCOMPATIBLE_COMBINATION,
                    message=(
                        "total=True measures are ignored when combined with "
                        "period-over-period or cumulative metrics in the same query"
                    ),
                    hint=(
                        "Drop total=True from the affected measures, or remove the "
                        "PoP/cumulative metric from this query."
                    ),
                    context={
                        "has_totals": True,
                        "has_pop": resolved.has_pop,
                        "has_cumulative": resolved.has_cumulative,
                    },
                )
            )

    # Rule 2b (raising): a derived metric over a window metric is a placeholder
    # until the window pass resolves it. The planner projects it with the window
    # metric's *base measure* inlined — an expression that only becomes valid
    # once the window pass drops that column, projects the base measure into
    # ``window_base``, and rebuilds the derived expression around an inline
    # window call in the outer SELECT.
    #
    # Any wrapper running before it materializes that placeholder into a CTE of
    # its own, where the reference resolves to nothing (or, on engines with
    # lateral column aliases, to a sibling alias, which is how the combination
    # appeared to work on DuckDB while failing everywhere else). A direct window
    # metric survives this because its column *is* the base measure's value, so
    # the window pass can take it by alias; a derived one carries the whole
    # expression instead, with no base value to lift out.
    ddm_metrics = sorted(
        m.name for m in resolved.measures if _ddm_window_components(m, resolved.metric_components)
    )
    if ddm_metrics:
        blocking = sorted(
            name
            for name in (PASS_FILTER_CONTEXT, PASS_PERIOD_OVER_PERIOD, PASS_TOTALS, PASS_CUMULATIVE)
            if name not in suppressed
            and (p := by_name.get(name)) is not None
            and p.applies(resolved)
        )
        # CFL is not a pass — the planner picks it before any of these run — but
        # it lands the window pass on a ``composite_01`` CTE just the same.
        if resolved.requires_cfl or resolved.dimensions_exclude:
            blocking.append("a multi-fact (CFL) plan")
        if blocking:
            listed = ", ".join(f"'{m}'" for m in ddm_metrics)
            raise ResolutionError(
                [
                    SemanticError(
                        code="INCOMPATIBLE_COMBINATION",
                        message=(
                            f"Metric(s) {listed} combine a window metric into a derived "
                            f"expression, which is computed in the final projection. That "
                            f"cannot be combined with {', '.join(blocking)}, whose own CTE "
                            f"would have to materialize the expression before the window "
                            f"function it contains exists."
                        ),
                        path="select.measures",
                        hint=(
                            "Query the derived metric on its own, or drop the "
                            "total / period-over-period / cumulative measure from this query."
                        ),
                        context={"metrics": ddm_metrics, "conflictsWith": blocking},
                    )
                ]
            )

    # Rule 3 (raising): grain dedup splits the projection across CTEs keyed on
    # the query grain. Every wrapper in ``incompatible_with`` restructures that
    # same projection, and ROLLUP/CUBE changes the grain itself, so the join
    # back would silently mismatch. Suppressing either side would hand back an
    # inflated number, which is the exact defect this pass exists to prevent —
    # so this rule raises instead of warning.
    dedup = by_name.get(PASS_GRAIN_DEDUP)
    if dedup is not None and dedup.applies(resolved):
        blocking = sorted(
            name
            for name in dedup.incompatible_with
            if (p := by_name.get(name)) is not None
            and p.applies(resolved)
            and _conflicts_with_dedup(name, resolved)
        )
        if resolved.grouping is not None:
            blocking.append(f"grouping: {resolved.grouping.value}")
        if blocking:
            listed = ", ".join(f"'{m}'" for m in sorted(resolved.dedup_targets))
            msg = (
                f"Measure(s) {listed} are sourced from an object whose rows this "
                f"query's joins replicate, so they must be aggregated over "
                f"deduplicated rows. That rewrite cannot yet be combined with "
                f"{', '.join(blocking)}. Query them separately, or set "
                f"allowFanOut: true to aggregate the duplicated rows as-is."
            )
            raise GrainDedupUnsupportedError(msg)

        warnings.append(dedup_warning(resolved.dedup_targets))

    return CompatibilityResult(warnings=warnings, suppressed=frozenset(suppressed))


def apply_aggregate_passes(ast: Select, ctx: CompileContext) -> Select:
    """Run the aggregate-mode passes against ``ast``.

    Records compatibility warnings on ``ctx.resolved.warnings`` (preserving
    the previous ordering) and applies each applicable, non-suppressed pass
    in declared order.
    """
    passes = build_default_passes()
    compat = evaluate_compatibility(ctx.resolved, passes)
    ctx.resolved.warnings.extend(compat.warnings)

    # A measure reading both a base-grain column and a replicated one is left
    # to compile - it is the extended-price pattern - but the same shape is
    # wrong when the replicated column is a magnitude rather than a rate, and
    # the declarations do not say which. Warn unless the query has said the
    # duplication is intended. Checked here rather than inside
    # ``evaluate_compatibility`` because it needs the model, which that
    # function (and its callers) deliberately do not carry.
    # Period-over-period rebuilds its FROM from a date spine, joining the fact
    # tables afresh. An anchored measure's aggregate reads conformed GROUP BY
    # subqueries, which that FROM does not carry and cannot, so re-projecting it
    # there names a table out of scope. Refused rather than emitted, the same
    # treatment grain dedup already gives period-over-period for the same
    # reason: the wrapper rebuilds a FROM the rewrite's output cannot serve.
    if ctx.resolved.has_pop and ctx.resolved.anchored_measures:
        anchored = sorted(ctx.resolved.anchored_measures)
        listed = ", ".join(f"'{name}'" for name in anchored)
        raise ResolutionError(
            [
                SemanticError(
                    code="INCOMPATIBLE_COMBINATION",
                    message=(
                        f"Measure(s) {listed} are anchored, so their expression reads "
                        f"columns conformed into subqueries. A period-over-period metric "
                        f"rebuilds the query's FROM from a date spine, which cannot carry "
                        f"those subqueries."
                    ),
                    path="select.measures",
                    hint=(
                        "Query the period-over-period metric without the anchored "
                        "measure, or compare periods on a measure its own fact reaches."
                    ),
                )
            ]
        )

    if not ctx.resolved.allow_fan_out:
        flagged = mixed_grain_measures(ctx.resolved, ctx.model)
        if flagged:
            ctx.resolved.warnings.append(mixed_grain_warning(flagged))

    result = ast
    for compiler_pass in passes:
        if compiler_pass.name in compat.suppressed:
            continue
        if compiler_pass.applies(ctx.resolved):
            result = compiler_pass.run(result, ctx)
    return result
