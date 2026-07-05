"""Fanout detection: identifies join paths that cause row multiplication."""

from __future__ import annotations

import re

from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.resolution import ResolvedQuery
from orionbelt.models.semantic import Cardinality, SemanticModel
from orionbelt.models.warnings import WarningCode, warning


class FanoutError(Exception):
    """Raised when a join path causes row multiplication (fanout) for a measure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _step_causes_fanout(step: JoinStep) -> bool:
    """Check whether a single join step causes fanout.

    - many-to-many: always fanout
    - many-to-one + reversed (traversed as one-to-many): fanout
    - one-to-one: never fanout
    - many-to-one (forward): never fanout
    """
    if step.cardinality == Cardinality.MANY_TO_MANY:
        return True
    return step.cardinality == Cardinality.MANY_TO_ONE and step.reversed


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

    errors: list[str] = []

    # Dimension objects in the query (these become GROUP BY columns)
    dim_objects = {d.object_name for d in resolved.dimensions}

    # Build a set of measure names to check (direct + metric components)
    measures_to_check: list[str] = []
    for m in resolved.measures:
        if m.component_measures:
            measures_to_check.extend(m.component_measures)
        else:
            measures_to_check.append(m.name)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_measures: list[str] = []
    for name in measures_to_check:
        if name not in seen:
            seen.add(name)
            unique_measures.append(name)

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
            _graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)
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
            col_refs = re.findall(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}", model_measure.expression)
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

                    errors.append(
                        f"Measure '{measure_name}' would be inflated by the selected "
                        f"dimensions: reaching them requires a one-to-many join (from "
                        f"'{step.from_object}' to '{step.to_object}') that duplicates "
                        "its rows, so its totals would be overcounted. "
                        f"Remove '{measure_name}' or the dimensions that force that "
                        "join, or set allowFanOut: true on the measure if the "
                        "duplication is intended."
                    )

    if errors:
        raise FanoutError("; ".join(errors))
