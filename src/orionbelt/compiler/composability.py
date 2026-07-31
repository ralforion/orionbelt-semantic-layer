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

from orionbelt.compiler.grain_dedup import (
    MULTIPLICITY_SAFE_AGGREGATIONS,
    auxiliary_references,
)
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


def measure_join_requirements(model: SemanticModel, name: str) -> set[str]:
    """Objects a measure needs *joined* without being sourced from them.

    A ``withinGroup`` column becomes the aggregate's ``ORDER BY``, so the
    compiler adds its data object to the query's required objects even though
    the measure reads no value from it. If that object is not reachable, the
    query fails with ``UNREACHABLE_REQUIRED_OBJECT`` — so ACR has to weigh it
    alongside the value sources, or it advertises a measure that cannot be
    planned.
    """
    measure = model.effective_measures.get(name)
    if measure is None or measure.within_group is None:
        return set()
    view = measure.within_group.column.view
    return {view} if view else set()


def metric_join_requirements(model: SemanticModel, name: str) -> set[str]:
    """``measure_join_requirements`` across every measure a metric reaches."""
    result: set[str] = set()
    for component in metric_leaf_measures(model, name):
        result |= measure_join_requirements(model, component)
    return result


def measure_source_objects(model: SemanticModel, name: str) -> set[str]:
    """Data objects a measure aggregates over (source columns + expression refs)."""
    m = model.effective_measures.get(name)
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


def metric_leaf_measures(model: SemanticModel, name: str) -> set[str]:
    """Every *measure* a metric depends on, following nested metrics.

    ``metric_measure_names`` returns whatever the expression references, which
    may itself be a metric. Resolving only that one level made a metric wrapping
    a metric look like it had no sources at all, so both the reachability checks
    below silently passed it.
    """
    leaves: set[str] = set()
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for ref in metric_measure_names(model, current):
            if ref in model.metrics:
                pending.append(ref)
            else:
                leaves.add(ref)
    return leaves


def metric_source_objects(model: SemanticModel, name: str) -> set[str]:
    """Data objects a metric ultimately aggregates over (via its measures)."""
    objects: set[str] = set()
    for measure_name in metric_leaf_measures(model, name):
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
            if ref in self.model.effective_measures:
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
        if anchor_type in (None, "measure") and name in self.model.effective_measures:
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

        # Empty anchor -> a fresh query: everything is composable, except a
        # measure whose join-only objects cannot be reached at all, or whose own
        # clauses force a join that replicates its source. Both are properties of
        # the model, so they hold with no anchor too.
        if not anchor:
            return ComposablesResult(
                anchor_objects=[],
                dimensions=sorted(self.model.dimensions),
                measures=sorted(
                    name
                    for name in self.model.effective_measures
                    if self._join_requirements_reachable(
                        measure_join_requirements(self.model, name),
                        measure_source_objects(self.model, name),
                    )
                    and not self._measure_blocked(name, set())
                ),
                metrics=sorted(
                    name
                    for name in self.model.metrics
                    if self._join_requirements_reachable(
                        metric_join_requirements(self.model, name),
                        metric_source_objects(self.model, name),
                    )
                    and not self._metric_blocked(name, set())
                ),
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
        for name in self.model.effective_measures:
            sources = measure_source_objects(self.model, name)
            if not self._join_requirements_reachable(
                measure_join_requirements(self.model, name), anchor | sources
            ):
                continue
            if self._measure_blocked(name, anchor):
                continue
            status = self._measure_status(sources, anchor, spine)
            if status == "direct":
                measures.append(name)
            elif status == "cfl":
                cfl_measures.append(name)

        metrics: list[str] = []
        cfl_metrics: list[str] = []
        for name in self.model.metrics:
            sources = metric_source_objects(self.model, name)
            if not self._join_requirements_reachable(
                metric_join_requirements(self.model, name), anchor | sources
            ):
                continue
            if self._metric_blocked(name, anchor):
                continue
            status = self._measure_status(sources, anchor, spine)
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

    def _join_requirements_reachable(self, required: set[str], context: set[str]) -> bool:
        """Whether the planner could reach a measure's join-only objects.

        A ``withinGroup`` column becomes the aggregate's ``ORDER BY``, so the
        compiler adds its data object to the query's required objects even
        though the measure reads no value from it. Unreachable, that raises
        ``UNREACHABLE_REQUIRED_OBJECT`` — so ACR has to weigh it alongside the
        value sources or it advertises a measure that cannot be planned.

        Nothing to reach is trivially satisfiable. Otherwise some single root
        has to cover the measure's own objects and the ones it merely needs
        joined, which is the condition the planner applies before raising.
        """
        if not required:
            return True
        return self._has_common_root(context | required)

    def _dedup_disposition(self, name: str, drivers: set[str]) -> str | None:
        """What ``compiler.grain_dedup`` would do with this measure at this anchor.

        Returns ``None`` when the pass leaves it alone, ``"dedup"`` when it would
        be aggregated over deduplicated rows, and ``"refused"`` when the rewrite
        raises instead.

        *drivers* are the objects whose presence forces a join: the anchor,
        plus - for a metric component - its sibling components' sources, since
        those all land in one query. The measure's own outside references are
        added below, because the compiler joins those too: it re-anchors the base
        object to reach a filter's data object rather than dropping the filter.
        Judging on the anchor alone missed every anchor that does not already
        reach the measure's source, the empty anchor included.

        Decided statically, from the same declarations the compiler uses, so ACR
        stays a pure read over the model rather than invoking the planner.
        """
        measure = self.model.effective_measures.get(name)
        if measure is None or measure.allow_fan_out or measure.distinct:
            return None
        if measure.aggregation.lower() in MULTIPLICITY_SAFE_AGGREGATIONS:
            return None

        sources = measure_source_objects(self.model, name)
        if not sources:
            return None

        referenced = {obj for objs in auxiliary_references(measure).values() for obj in objs}
        outside = referenced - sources

        # Everything that forces a join: the callers' drivers, plus whatever
        # this measure's own clauses drag in.
        forcing = drivers | outside
        replicated = {
            obj for obj in sources if any(obj in self.graph.descendants(d) for d in forcing)
        }
        if sources != replicated:
            # Not replicated here, so the pass never runs on it.
            return None

        # Several replicated sources, or a clause reaching outside the one being
        # deduplicated: the rewrite cannot express either and raises.
        if len(sources) > 1 or outside:
            return "refused"
        return "dedup"

    def _measure_blocked(self, name: str, anchor: set[str]) -> bool:
        """A measure is only excluded when the rewrite would refuse it outright."""
        return self._dedup_disposition(name, anchor) == "refused"

    def _metric_blocked(self, name: str, anchor: set[str]) -> bool:
        """A metric is excluded when the rewrite would refuse it.

        The planner inlines a metric's components into one expression — nested
        derived metrics included — and the rewrite splits the deduplicated ones
        back out into their own CTE, recomputing the expression over the
        results. So ``"dedup"`` is not disqualifying on its own, exactly as for
        a plain measure.

        A measure reached through a metric that has its *own wrapper*
        (cumulative, window, period-over-period) still is: that wrapper rebuilds
        the aggregate from the fact tables, which a dedup CTE cannot serve.

        Every leaf measure's sources count as drivers for every other: they all
        land in one query, so a component on the *one* side is replicated by a
        sibling on the many side even when the anchor reaches neither.
        """
        leaves = metric_leaf_measures(self.model, name)
        behind_wrapper = self._wrapper_backed_measures(name)
        drivers = set(anchor)
        for leaf in leaves:
            drivers |= measure_source_objects(self.model, leaf)
        for leaf in leaves:
            disposition = self._dedup_disposition(leaf, drivers)
            if disposition == "refused" or (disposition == "dedup" and leaf in behind_wrapper):
                return True
        return False

    def _wrapper_backed_measures(self, name: str) -> set[str]:
        """Measures *name* reaches only through a metric with its own wrapper.

        Walks the same way the compiler expands: a derived reference is inlined,
        so its leaves are reached directly; a cumulative / window /
        period-over-period reference is not, so everything under it is served by
        that metric's wrapper instead.
        """
        behind: set[str] = set()
        seen: set[str] = set()
        pending = [name]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            for ref in metric_measure_names(self.model, current):
                referenced = self.model.metrics.get(ref)
                if referenced is None:
                    continue
                if referenced.type == MetricType.DERIVED:
                    pending.append(ref)
                else:
                    behind |= metric_leaf_measures(self.model, ref)
        return behind

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
