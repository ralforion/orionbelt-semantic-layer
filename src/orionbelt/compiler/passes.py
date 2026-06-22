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
from orionbelt.compiler.pop_wrap import wrap_with_pop
from orionbelt.compiler.resolution import ResolvedQuery
from orionbelt.compiler.total_wrap import wrap_with_totals
from orionbelt.compiler.window_wrap import window_pass_applies, wrap_with_window
from orionbelt.dialect.base import Dialect
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import DataObject, SemanticModel
from orionbelt.models.warnings import WarningCode, warning

# Canonical pass names. Used as identifiers in ordering, compatibility
# metadata, and tests — keep them stable.
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

    result = ast
    for compiler_pass in passes:
        if compiler_pass.name in compat.suppressed:
            continue
        if compiler_pass.applies(ctx.resolved):
            result = compiler_pass.run(result, ctx)
    return result
