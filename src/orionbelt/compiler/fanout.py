"""Fanout detection: identifies join paths that cause row multiplication."""

from __future__ import annotations

from typing import Any

from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.metric_expansion import metric_leaf_measure_names
from orionbelt.compiler.resolution import ResolvedQuery
from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import find_qualified_refs
from orionbelt.models.semantic import Cardinality, SemanticModel
from orionbelt.models.warnings import WarningCode, warning


class FanoutError(Exception):
    """Raised when a join path causes row multiplication (fanout) for a measure.

    Carries one :class:`SemanticError` per refused measure, like
    ``ResolutionError``, so every surface can report a stable ``code`` with its
    ``hint`` and ``context``. Subclasses set their own class-level ``code``.
    """

    code = "FANOUT"

    def __init__(
        self,
        message: str = "",
        *,
        errors: list[SemanticError] | None = None,
        hint: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if errors is None:
            errors = [SemanticError(code=self.code, message=message, hint=hint, context=context)]
        self.errors = errors
        self.message = "; ".join(e.message for e in errors)
        super().__init__(self.message)


def _step_causes_fanout(step: JoinStep) -> bool:
    """Check whether a single join step causes fanout.

    - many-to-many: always fanout
    - many-to-one + reversed (traversed as one-to-many): fanout
    - one-to-one: never fanout
    - many-to-one (forward): never fanout
    - an unnest: multiplies its parent, but not here — see below
    """
    if step.nested:
        # An unnest does multiply rows, and the multiplied side is the parent.
        # It is not reported here because it is not a refusal: the parent's
        # measures are deduplicated by ``grain_dedup`` in a CTE of their own,
        # which is the same answer a forward many-to-one already gets. What
        # this function reports is row multiplication nothing downstream can
        # repair.
        return False
    if step.cardinality == Cardinality.MANY_TO_MANY:
        return True
    return step.cardinality == Cardinality.MANY_TO_ONE and step.reversed


def _reject_sibling_unnests(resolved: ResolvedQuery, model: SemanticModel) -> None:
    """Refuse a measure on a nested object when a second one is also unnested.

    Two laterals off the same parent are a genuine cross product of two child
    grains: measured against 14,673 demo charges, grouping by label value *and*
    credit type overstates total spend by 3.33x, and every cell is wrong. Each
    nested object's elements are repeated once per element of the other, and
    nothing can deduplicate them — a nested object has no key, by design.

    Only a *measure* on a nested object is refused. Dimensions from two of them
    are safe here for the same reason the refusal is needed: it is the parent's
    identity that the deduplicating CTE keys on, and that identity is unaffected
    by how many arrays multiplied it, so a parent-side measure grouped by both
    stays correct.

    One leg per nested object, unioned, is what this becomes: per-group values
    are then exact, though they still overlap, because a charge belongs to every
    group its arrays put it in. Refusing is what the compiler does until that
    exists, since the alternative is a number wrong by a factor that varies with
    how many elements each array carries.
    """
    unnested = sorted({step.to_object for step in resolved.join_steps if step.nested})
    if len(unnested) < 2:
        return
    effective = model.effective_measures
    on_nested = sorted(
        {
            name
            for name in _measures_to_check(resolved)
            if (m := effective.get(name)) is not None
            and not m.allow_fan_out
            and m.source_objects & set(unnested)
        }
    )
    if not on_nested:
        return
    listed = ", ".join(f"'{name}'" for name in on_nested)
    objects = ", ".join(f"'{name}'" for name in unnested)
    raise FanoutError(
        errors=[
            SemanticError(
                code="NESTED_FANOUT",
                message=(
                    f"Measure(s) {listed} are sourced from a nested data object, and this "
                    f"query unnests {objects} together. Each one's elements are repeated "
                    f"once per element of the other, and a nested object has no key to "
                    f"deduplicate them by — its rows exist only inside its parent's. Query "
                    f"one nested object at a time, or set allowFanOut: true if the "
                    f"multiplication is intended."
                ),
                context={"measures": on_nested, "nestedObjects": unnested},
            )
        ]
    )


def _measures_to_check(resolved: ResolvedQuery) -> list[str]:
    """Every measure the query evaluates: selected ones and metric leaves.

    Deduplicated, order preserved, because a metric and a selected measure can
    name the same leaf and reporting it twice reads as two problems.
    """
    names: list[str] = []
    for m in resolved.measures:
        if m.component_measures:
            names.extend(metric_leaf_measure_names(m, resolved.metric_components))
        else:
            names.append(m.name)
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


_ADDITIVE_AGGREGATIONS = frozenset({"sum", "count", "count_distinct"})


def _fanout_resolved_by_group_by(
    step: JoinStep,
    multiplied_object: str,
    dim_objects: set[str],
    graph: JoinGraph,
) -> str | None:
    """Check if a fanout-causing step is resolved by GROUP BY dimensions.

    A reversed many-to-one join through a **junction/bridge table** is
    harmless when dimensions from the "other side" of the junction are
    in the query's GROUP BY.  The junction table ensures unique
    ``(base_row, dimension_value)`` tuples, so the cross-product
    produced by the join is fully disambiguated by the GROUP BY.

    Example: ``Movies ← Movie Directors → Directors`` — the reversed
    join ``Movies ← Movie Directors`` multiplies movie rows by the
    number of directors, but grouping by ``Director`` makes each
    ``(movie, director)`` pair unique, so ``COUNT(movie_id)`` is correct.

    The check identifies junction tables by verifying the "new" table
    (the one causing multiplication) reaches objects *beyond* the
    multiplied table via directed joins.  If any of those "other side"
    objects have a dimension in the GROUP BY, the fanout is resolved.

    Returns the junction table name if resolved, ``None`` otherwise.
    """
    # The "new" table being joined (causes multiplication of the base)
    new_table = step.from_object if step.reversed else step.to_object

    # Objects reachable from the new table via directed joins
    reachable = graph.descendants(new_table) | {new_table}

    # "Other side" = reachable objects excluding the multiplied base and
    # the junction table itself.  These are the tables whose dimensions
    # can disambiguate the fanout.
    other_sides = reachable - {multiplied_object} - {new_table}

    if other_sides & dim_objects:
        return new_table
    return None


def detect_fanout(resolved: ResolvedQuery, model: SemanticModel) -> None:
    """Check all measures for fanout and raise ``FanoutError`` if detected.

    For each measure (and each metric component), skip if
    ``allow_fan_out=True`` on the model measure.  Walk
    ``resolved.join_steps`` — if any step causes fanout for that
    measure's source object, collect an error.

    **Junction table exception:** when a reversed many-to-one step goes
    through a bridge/junction table that connects the measure's source
    to a dimension table, and that dimension is in the query's GROUP BY,
    the fanout is harmless and is allowed without error.
    """
    if not resolved.join_steps:
        return

    _reject_sibling_unnests(resolved, model)

    errors: list[SemanticError] = []

    # Dimension objects in the query (these become GROUP BY columns)
    dim_objects = {d.object_name for d in resolved.dimensions}

    # Direct measures plus metric components, followed through nested derived
    # metrics — those inline into the same expression, so their leaves face the
    # same replicating join.
    unique_measures = _measures_to_check(resolved)

    # Build global column lookup for expression-based measures
    global_columns: dict[str, str] = {}
    for obj_name, obj in model.data_objects.items():
        for col_name in obj.columns:
            global_columns[col_name] = obj_name

    # Lazy-create the JoinGraph (only when a fanout step is found)
    _graph: JoinGraph | None = None

    def _get_graph() -> JoinGraph:
        nonlocal _graph
        if _graph is None:
            _graph = JoinGraph.of(model, use_path_names=resolved.use_path_names or None)
        return _graph

    effective_measures = model.effective_measures
    for measure_name in unique_measures:
        model_measure = effective_measures.get(measure_name)
        if model_measure is None:
            continue
        if model_measure.allow_fan_out:
            continue

        # Determine which data objects this measure references
        source_objects: set[str] = set()
        for cref in model_measure.columns:
            if cref.view:
                source_objects.add(cref.view)
        if model_measure.expression:
            col_refs = find_qualified_refs(model_measure.expression)
            for obj_name, _col_name in col_refs:
                source_objects.add(obj_name)

        if not source_objects:
            continue

        # Check each join step for fanout
        for step in resolved.join_steps:
            if _step_causes_fanout(step):
                # Determine which side gets row multiplication.
                # When reversed, from_object/to_object represent the
                # declared direction (swapped); the actual traversal
                # origin (whose rows get multiplied) is to_object.
                multiplied_object = step.to_object if step.reversed else step.from_object
                if multiplied_object in source_objects:
                    # Check if the junction table pattern resolves the fanout
                    junction = _fanout_resolved_by_group_by(
                        step, multiplied_object, dim_objects, _get_graph()
                    )
                    if junction is not None:
                        # Warn for additive aggregations: grand totals may
                        # be inflated because each fact row contributes to
                        # multiple (dimension1, dimension2, ...) groups.
                        agg = model_measure.aggregation.lower()
                        if agg in _ADDITIVE_AGGREGATIONS:
                            resolved.warnings.append(
                                warning(
                                    code=WarningCode.FAN_TRAP_RISK,
                                    message=(
                                        f"Measure '{measure_name}' ({agg.upper()}): "
                                        f"cross-join through '{junction}' — per-group "
                                        f"values are correct but grand totals may be "
                                        f"inflated"
                                    ),
                                    hint=(
                                        "Add the junction-table dimension to the GROUP BY, "
                                        "or use the Composite Fact Layer pattern."
                                    ),
                                    context={
                                        "measure": measure_name,
                                        "aggregation": agg.upper(),
                                        "junction": junction,
                                    },
                                )
                            )
                        continue

                    forcing = _dimensions_behind(step, resolved)
                    named = "the dimensions that force that join"
                    if forcing:
                        named += " (" + ", ".join(f"'{d}'" for d in forcing) + ")"
                    errors.append(
                        SemanticError(
                            code=FanoutError.code,
                            message=(
                                f"Measure '{measure_name}' would be inflated by the selected "
                                f"dimensions: reaching them requires a one-to-many join (from "
                                f"'{step.from_object}' to '{step.to_object}') that duplicates "
                                "its rows, so its totals would be overcounted. "
                                f"Remove '{measure_name}' or {named}, or set allowFanOut: "
                                "true on the measure if the duplication is intended."
                            ),
                            context={
                                "measure": measure_name,
                                "fromObject": step.from_object,
                                "toObject": step.to_object,
                                "dimensions": forcing,
                            },
                        )
                    )

    if errors:
        raise FanoutError(errors=errors)


def _dimensions_behind(step: JoinStep, resolved: ResolvedQuery) -> list[str]:
    """Selected dimensions the query can only reach through ``step``.

    Join steps are ordered from the base object outward, so one pass collects
    every object joined in after ``step`` from the side it brings in.
    """
    behind = {step.from_object if step.reversed else step.to_object}
    for other in resolved.join_steps:
        origin = other.to_object if other.reversed else other.from_object
        if origin in behind:
            behind.add(other.from_object if other.reversed else other.to_object)
    return [d.name for d in resolved.dimensions if d.object_name in behind]
