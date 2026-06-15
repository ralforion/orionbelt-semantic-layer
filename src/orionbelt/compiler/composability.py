"""Artefacts Composability Resolution (ACR).

Given an *anchor* (the artefacts a consumer has already selected, or a whole
in-progress query), ACR resolves the set of other artefacts that can still be
added to the query and yield a valid, fanout-free result.

The engine reuses the same directed join-graph reachability the compiler's
planner relies on (``JoinGraph.descendants`` / ``find_common_root``), so any
artefact ACR reports as composable is guaranteed to compile:

* **Dimensions** are groupable when they sit on a data object reachable from
  the query's grain via fanout-safe (many-to-one, source -> joinTo) joins.
* **Measures / metrics** are usable when their source fact shares a common root
  with the current anchor (a single-fact / star query)...
* ...or, when the fact is independent but still reaches the current grouping
  dimensions, via the Composite Fact Layer (CFL, UNION ALL). Those are reported
  separately as ``cfl_measures`` / ``cfl_metrics``.

This module is a pure read over the loaded :class:`SemanticModel`; it does not
invoke the compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from orionbelt.compiler.graph import JoinGraph
from orionbelt.models.query import CoalesceDimension, QueryObject, UsePathName
from orionbelt.models.semantic import MetricType, SemanticModel

# Measure expression column refs: ``{[DataObject].[Column]}``
_MEASURE_COL_REF = re.compile(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}")
# Derived metric measure refs: ``{[Measure Name]}``
_METRIC_MEASURE_REF = re.compile(r"\{\[([^\]]+)\]\}")


@dataclass(frozen=True)
class ComposablesResult:
    """The composable set resolved for an anchor."""

    anchor_objects: list[str]
    dimensions: list[str]
    measures: list[str]
    metrics: list[str]
    cfl_measures: list[str] = field(default_factory=list)
    cfl_metrics: list[str] = field(default_factory=list)


def measure_source_objects(model: SemanticModel, name: str) -> set[str]:
    """Data objects a measure aggregates over (source columns + expression refs)."""
    m = model.measures.get(name)
    if m is None:
        return set()
    objects = {c.view for c in m.columns if c.view}
    if m.expression:
        objects |= {obj for obj, _ in _MEASURE_COL_REF.findall(m.expression)}
    return objects


def metric_measure_names(model: SemanticModel, name: str) -> set[str]:
    """Measure names a metric depends on."""
    met = model.metrics.get(name)
    if met is None:
        return set()
    names: set[str] = set()
    if met.type == MetricType.DERIVED and met.expression:
        names |= set(_METRIC_MEASURE_REF.findall(met.expression))
    if met.measure:
        names.add(met.measure)
    return names


def metric_source_objects(model: SemanticModel, name: str) -> set[str]:
    """Data objects a metric ultimately aggregates over (via its measures)."""
    objects: set[str] = set()
    for measure_name in metric_measure_names(model, name):
        objects |= measure_source_objects(model, measure_name)
    return objects


def _dimension_object(model: SemanticModel, name: str) -> str | None:
    dim = model.dimensions.get(name)
    return dim.view if dim is not None else None


class ComposabilityResolver:
    """Resolves the composable set for an anchor over a single model."""

    def __init__(
        self,
        model: SemanticModel,
        use_path_names: list[UsePathName] | None = None,
    ) -> None:
        self.model = model
        self.graph = JoinGraph(model, use_path_names)
        # reach[F] = objects a single base fact F can serve (itself + everything
        # reachable via fanout-safe directed joins). Matches find_common_root.
        self._reach: dict[str, set[str]] = {
            obj: {obj} | self.graph.descendants(obj) for obj in model.data_objects
        }

    # -- reachability helpers ------------------------------------------------

    def _has_common_root(self, objects: set[str]) -> bool:
        """True if some single fact can reach every object in *objects*."""
        objects = objects & set(self._reach)
        if not objects:
            return True
        return any(objects <= reach for reach in self._reach.values())

    def _reaches_all(self, fact: str, targets: set[str]) -> bool:
        """True if base fact *fact* reaches every object in *targets*."""
        return targets <= self._reach.get(fact, set())

    # -- anchor resolution ---------------------------------------------------

    def objects_from_query(self, query: QueryObject) -> tuple[set[str], set[str]]:
        """Split a query's selection into (dimension objects, measure objects)."""
        dim_objects: set[str] = set()
        for entry in query.select.dimensions:
            names = entry.coalesce if isinstance(entry, CoalesceDimension) else [entry]
            for dim_name in names:
                obj = _dimension_object(self.model, dim_name)
                if obj:
                    dim_objects.add(obj)

        measure_objects: set[str] = set()
        for ref in query.select.measures:
            if ref in self.model.measures:
                measure_objects |= measure_source_objects(self.model, ref)
            elif ref in self.model.metrics:
                measure_objects |= metric_source_objects(self.model, ref)
        return dim_objects, measure_objects

    def objects_from_anchor_name(
        self, name: str, anchor_type: str | None = None
    ) -> tuple[set[str], set[str]]:
        """Resolve a single named anchor into (dimension objects, measure objects).

        A data object or dimension anchor defines the query *grain* (dimension
        side); a measure or metric anchor defines a *fact* leg (measure side).
        When *anchor_type* is omitted the name is looked up in dimensions,
        measures, metrics, then data objects, in that order.
        """
        if anchor_type in (None, "dimension") and name in self.model.dimensions:
            obj = _dimension_object(self.model, name)
            return ({obj} if obj else set()), set()
        if anchor_type in (None, "measure") and name in self.model.measures:
            return set(), measure_source_objects(self.model, name)
        if anchor_type in (None, "metric") and name in self.model.metrics:
            return set(), metric_source_objects(self.model, name)
        if anchor_type in (None, "dataObject") and name in self.model.data_objects:
            return {name}, set()
        return set(), set()

    # -- core resolution -----------------------------------------------------

    def resolve(
        self,
        dim_objects: set[str],
        measure_objects: set[str],
    ) -> ComposablesResult:
        """Resolve composable artefacts for the given anchor objects.

        *dim_objects* are the grouping (grain) objects; *measure_objects* are the
        facts of measures already selected (each acts as a CFL leg).
        """
        anchor = dim_objects | measure_objects
        anchor_objects = sorted(anchor)

        # Empty anchor -> a fresh query: everything is composable.
        if not anchor:
            return ComposablesResult(
                anchor_objects=[],
                dimensions=sorted(self.model.dimensions),
                measures=sorted(self.model.measures),
                metrics=sorted(self.model.metrics),
            )

        spine = dim_objects  # grouping dimensions shared across all legs
        leg_facts = measure_objects  # facts of measures already in the query

        # Dimensions: a new dimension object must be groupable at the current
        # grain. With measures present it must be reachable from every existing
        # leg fact; without measures it must merely co-root with the spine.
        dimensions = [
            name
            for name, dim in self.model.dimensions.items()
            if self._dimension_composable(dim.view, spine, leg_facts)
        ]

        measures: list[str] = []
        cfl_measures: list[str] = []
        for name in self.model.measures:
            status = self._measure_status(measure_source_objects(self.model, name), anchor, spine)
            if status == "direct":
                measures.append(name)
            elif status == "cfl":
                cfl_measures.append(name)

        metrics: list[str] = []
        cfl_metrics: list[str] = []
        for name in self.model.metrics:
            status = self._measure_status(metric_source_objects(self.model, name), anchor, spine)
            if status == "direct":
                metrics.append(name)
            elif status == "cfl":
                cfl_metrics.append(name)

        return ComposablesResult(
            anchor_objects=anchor_objects,
            dimensions=sorted(dimensions),
            measures=sorted(measures),
            metrics=sorted(metrics),
            cfl_measures=sorted(cfl_measures),
            cfl_metrics=sorted(cfl_metrics),
        )

    def _dimension_composable(self, obj: str, spine: set[str], leg_facts: set[str]) -> bool:
        if leg_facts:
            # Must be groupable across every existing measure leg.
            return all(self._reaches_all(fact, {obj}) for fact in leg_facts)
        # No measures yet: the new dimension must share a root with the spine.
        return self._has_common_root(spine | {obj})

    def _measure_status(
        self, source_objects: set[str], anchor: set[str], spine: set[str]
    ) -> str | None:
        """Classify a measure/metric as 'direct', 'cfl', or None (incompatible)."""
        if not source_objects:
            # No resolvable source (e.g. COUNT(*)-style): always combinable.
            return "direct"
        # Direct: the whole query stays single-fact (a common root covers all).
        if self._has_common_root(anchor | source_objects):
            return "direct"
        # CFL: each source fact independently reaches the current grain, so it
        # can join as a separate UNION ALL leg. With no grain yet, independent
        # facts still combine as grand-total legs.
        if not spine or all(self._reaches_all(fact, spine) for fact in source_objects):
            return "cfl"
        return None


def resolve_composables_for_query(model: SemanticModel, query: QueryObject) -> ComposablesResult:
    """Convenience: resolve composables for a whole in-progress query."""
    resolver = ComposabilityResolver(model, query.use_path_names or None)
    dim_objects, measure_objects = resolver.objects_from_query(query)
    return resolver.resolve(dim_objects, measure_objects)


def resolve_composables_for_anchors(
    model: SemanticModel, anchors: list[str], anchor_type: str | None = None
) -> ComposablesResult:
    """Convenience: resolve composables for one or more named anchors."""
    resolver = ComposabilityResolver(model)
    dim_objects: set[str] = set()
    measure_objects: set[str] = set()
    for name in anchors:
        dims, measures = resolver.objects_from_anchor_name(name, anchor_type)
        dim_objects |= dims
        measure_objects |= measures
    return resolver.resolve(dim_objects, measure_objects)
