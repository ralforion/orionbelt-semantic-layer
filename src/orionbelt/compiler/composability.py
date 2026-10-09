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
from orionbelt.models.expressions import find_qualified_refs
from orionbelt.models.query import CoalesceDimension, QueryObject, UsePathName
from orionbelt.models.roles import expand_role_objects, role_targets
from orionbelt.models.semantic import Metric, MetricType, SemanticModel

# Measure expression column refs: ``{[DataObject].[Column]}``
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

    A ``withinGroup`` column becomes the aggregate's ``ORDER BY`` and a
    computed column reads its neighbours directly, so the planner adds both to
    a query's required objects even though the measure takes no value from
    them. Unreachable, either one fails the query with
    ``UNREACHABLE_REQUIRED_OBJECT`` — so ACR has to weigh them alongside the
    value sources, or it advertises a measure that cannot be planned.

    Shared with the planner (:meth:`SemanticModel.measure_join_objects`) rather
    than reimplemented, because the two answering differently is exactly how
    ACR came to advertise what the compiler refuses.
    """
    return model.measure_join_objects(name)


def dimension_requirements(model: SemanticModel, name: str) -> set[str]:
    """Objects a dimension needs joined besides the one it belongs to.

    The objects its computed column reads (:meth:`SemanticModel.dimension_join_objects`),
    and its ``via`` object: the planner requires that one in any query naming
    the dimension, so a fact that cannot reach it cannot be grouped by it. A
    role (``via`` with a ``pathName``) needs no entry: discovery plans over
    :func:`expand_role_objects`, where its object is a leaf only ``via`` joins.
    """
    required = model.dimension_join_objects(name)
    waypoint = dimension_waypoint(model, name)
    return required | {waypoint} if waypoint else required


def dimension_waypoint(model: SemanticModel, name: str) -> str | None:
    """A dimension's ``via`` object, unless it is a role (see :func:`dimension_requirements`)."""
    dim = model.dimensions.get(name)
    if dim is None or not dim.via or dim.path_name is not None:
        return None
    return dim.via


def _per_objects(model: SemanticModel, met: Metric) -> set[str]:
    """The objects a reaggregate metric's ``per`` dimensions are read from.

    Their ``via`` objects are left to :func:`_per_waypoints`.
    """
    objects: set[str] = set()
    for dim_name in _per_dimensions(model, met):
        dim = model.dimensions[dim_name]
        if dim.view:
            objects |= {dim.view} | model.dimension_join_objects(dim_name)
    return objects


def _per_waypoints(model: SemanticModel, met: Metric) -> set[str]:
    """The ``via`` objects of a reaggregate metric's ``per`` dimensions.

    Waypoints of its first stage, as a query dimension's are of the query: that
    stage needs them joined over a single fact, and pads them over several.
    """
    return {w for n in _per_dimensions(model, met) if (w := dimension_waypoint(model, n))}


def _per_dimensions(model: SemanticModel, met: Metric) -> list[str]:
    names = [entry if entry in model.dimensions else entry.rpartition(":")[0] for entry in met.per]
    return [name for name in names if name in model.dimensions]


@dataclass(frozen=True)
class MetricLeg:
    """A measure a metric reaches, with what planning it needs (see :func:`metric_legs`)."""

    sources: frozenset[str]
    required: frozenset[str]
    #: Each reaggregate first stage it is computed in, outermost first, as
    #: (the facts that stage's query reads, its waypoints): the ``via``
    #: objects of its ``per`` dimensions and of every enclosing stage's, which
    #: group it too. Required over a single fact, padded over several.
    stages: tuple[tuple[frozenset[str], frozenset[str]], ...] = ()


def _stage_facts(model: SemanticModel, met: Metric) -> frozenset[str]:
    """The facts a reaggregate metric's first-stage query reads.

    Its measure as that query's own plan reads it (a nested reaggregate by its
    bottom measure; that one's ``having`` belongs to the stage below), and its
    ``having`` measures.
    """
    facts = metric_plan_sources(model, met.measure) if met.measure else set()
    for condition in met.having:
        facts |= measure_source_objects(model, condition.field)
    return frozenset(facts)


def metric_plan_sources(model: SemanticModel, name: str) -> set[str]:
    """The facts the query's own plan reads for a metric.

    Like :func:`metric_source_objects`, except that a reaggregate metric
    contributes only its bottom measure's: the plan carries it on that
    measure's leg, and its ``having`` measures are read in its first stage.
    """
    sources: set[str] = set()
    seen: set[str] = set()
    pending = [name]
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        met = model.metrics.get(ref)
        if met is None:
            sources |= measure_source_objects(model, ref)
        elif met.type == MetricType.REAGGREGATE:
            bottom = model.reaggregated_measure(ref)
            if bottom is not None:
                sources |= measure_source_objects(model, bottom[0])
        else:
            pending.extend(metric_measure_names(model, ref))
    return sources


#: A metric reached in :func:`metric_legs`: its name, the enclosing ``per``
#: objects and waypoints, and the enclosing stages (see :class:`MetricLeg`).
_LegState = tuple[
    str, frozenset[str], frozenset[str], tuple[tuple[frozenset[str], frozenset[str]], ...]
]


def metric_legs(model: SemanticModel, name: str) -> list[MetricLeg]:
    """Each measure a metric reaches, with its sources and join requirements.

    Each measure is planned on a leg of its own when the metric spans facts, so
    each has to reach only its own requirements: its ``measure_join_requirements``
    and the ``per`` objects of every reaggregate stage it is computed in - the
    stage's measure and its ``having`` measures alike, whether the metric is
    asked for directly or through a derived metric over it.
    """
    legs: dict[MetricLeg, None] = {}
    # A dependency shared by several formulas is walked once per enclosing
    # state, not once per path to it; this also ends a cycle.
    visited: set[_LegState] = set()
    pending: list[_LegState] = [(name, frozenset(), frozenset(), ())]
    while pending:
        state = pending.pop()
        if state in visited:
            continue
        visited.add(state)
        ref, per_objects, per_waypoints, stages = state
        met = model.metrics.get(ref)
        if met is None:
            if ref in model.effective_measures:
                leg = MetricLeg(
                    frozenset(measure_source_objects(model, ref)),
                    frozenset(measure_join_requirements(model, ref) | per_objects),
                    stages,
                )
                legs[leg] = None
            continue
        if met.type == MetricType.REAGGREGATE:
            # Its first stage is a query of its own over these measures. A
            # cumulative, window or period-over-period wrapper is not one here:
            # the window wraps the query's result, and the others either do so
            # too or are refused beside another fact anyway.
            per_objects |= _per_objects(model, met)
            per_waypoints |= _per_waypoints(model, met)
            stages = (*stages, (_stage_facts(model, met), per_waypoints - per_objects))
        pending.extend(
            (child, per_objects, per_waypoints, stages)
            for child in metric_measure_names(model, ref)
        )
    return list(legs)


def metric_join_requirements(model: SemanticModel, name: str) -> set[str]:
    """Every object some leg of the metric needs joined (see :func:`metric_legs`)."""
    result: set[str] = set()
    for leg in metric_legs(model, name):
        result |= leg.required
    return result


def measure_source_objects(model: SemanticModel, name: str) -> set[str]:
    """Data objects a measure aggregates over (source columns + expression refs)."""
    m = model.effective_measures.get(name)
    if m is None:
        return set()
    objects = {c.view for c in m.columns if c.view}
    if m.expression:
        objects |= {obj for obj, _ in find_qualified_refs(m.expression)}
    return objects


def metric_measure_names(model: SemanticModel, name: str) -> set[str]:
    """Measure names a metric depends on.

    A reaggregate metric's ``having`` conditions count: their measures are
    computed in its first stage like its own measure.
    """
    met = model.metrics.get(name)
    if met is None:
        return set()
    names: set[str] = set()
    if met.type == MetricType.DERIVED and met.expression:
        names |= set(_METRIC_MEASURE_REF.findall(met.expression))
    if met.measure:
        names.add(met.measure)
    names |= {condition.field for condition in met.having}
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
        # Discovery answers for the model the compiler plans over, where each
        # dimension role is a data object of its own (see ``models.roles``).
        # Reported anchors name the authored object rather than the alias.
        self._role_targets = role_targets(model)
        model = expand_role_objects(model)
        self.model = model
        self.graph = JoinGraph(model, use_path_names)
        # reach[F] = objects a single base fact F can serve (itself + everything
        # reachable via fanout-safe directed joins). Matches find_common_root.
        self._reach: dict[str, set[str]] = {
            obj: {obj} | self.graph.descendants(obj) for obj in model.data_objects
        }
        # The same, for a plan that can only *join*. A union leg is one: it
        # builds a star out of tables and has no unnest to reach a nested object
        # with, nor anything sitting behind one. Kept separate rather than
        # replacing ``_reach`` because a star planner does unnest, so a measure
        # reaching across a containment edge is answerable on its own and only
        # unanswerable as a leg.
        self._reach_no_unnest: dict[str, set[str]] = {
            obj: {obj} | self.graph.descendants_without_unnest(obj) for obj in model.data_objects
        }

    # -- reachability helpers ------------------------------------------------

    def _has_common_root(self, objects: set[str]) -> bool:
        """True if some single fact can reach every object in *objects*."""
        objects = objects & set(self._reach)
        if not objects:
            return True
        return any(objects <= reach for reach in self._reach.values())

    def _needs_unnest_to_connect(self, objects: set[str]) -> bool:
        """*objects* hang together, but only across a containment edge.

        Distinguished from genuinely independent facts, which no root reaches at
        all: these have one, reached by unnesting. A star can follow it and a
        union leg cannot, which is the whole difference this answers.
        """
        objects = objects & set(self._reach)
        if len(objects) <= 1:
            return False
        if any(objects <= reach for reach in self._reach_no_unnest.values()):
            return False
        return any(objects <= reach for reach in self._reach.values())

    def _reaches_all(self, fact: str, targets: set[str]) -> bool:
        """True if base fact *fact* reaches every object in *targets*."""
        return targets <= self._reach.get(fact, set())

    # -- anchor resolution ---------------------------------------------------

    def waypoints(self, dimension_names: list[str], others: set[str] | None = None) -> set[str]:
        """The ``via`` objects of *dimension_names* that only route them.

        A star plan needs each one joined; a CFL leg that cannot reach one
        projects NULL for its dimension instead. One that a dimension belongs to
        or reads, or that another anchor names (*others*), is a requirement in
        its own right and is left out.
        """
        vias = {w for n in dimension_names if (w := dimension_waypoint(self.model, n))}
        own = set(others or set())
        for name in dimension_names:
            dim = self.model.dimensions.get(name)
            if dim is not None and dim.view:
                own |= {dim.view} | self.model.dimension_join_objects(name)
        return vias - own

    def objects_from_query(self, query: QueryObject) -> tuple[set[str], set[str]]:
        """Split a query's selection into (dimension objects, measure objects)."""
        dim_objects: set[str] = set()
        for entry in query.select.dimensions:
            names = entry.coalesce if isinstance(entry, CoalesceDimension) else [entry]
            for dim_name in names:
                obj = _dimension_object(self.model, dim_name)
                if obj:
                    dim_objects.add(obj)
                    dim_objects |= dimension_requirements(self.model, dim_name)

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
            if not obj:
                return set(), set()
            # Same reading as the query-as-anchor path: a computed dimension
            # drags in whatever its expression reads, and an anchor that
            # forgets them offers pairings the planner refuses. The two entry
            # points answering differently is itself the bug — one is GET
            # /composables?anchor=, the other POST with a query.
            return {obj} | dimension_requirements(self.model, name), set()
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
        waypoints: set[str] | None = None,
    ) -> ComposablesResult:
        """Resolve composable artefacts for the given anchor objects.

        *dim_objects* are the grouping (grain) objects; *measure_objects* are the
        facts of measures already selected (each acts as a CFL leg).
        *waypoints* are those of *dim_objects* that only route a ``via``
        dimension (:meth:`waypoints`): required of a single-fact plan, not of a
        CFL leg. Left out, every one of *dim_objects* is required of every leg.
        """
        anchor = dim_objects | measure_objects
        anchor_objects = sorted({self._role_targets.get(obj, obj) for obj in anchor})

        # Empty anchor -> a fresh query: everything is composable, except a
        # measure whose join-only objects cannot be reached at all, or whose own
        # clauses force a join that replicates its source. Both are properties of
        # the model, so they hold with no anchor too.
        if not anchor:
            return ComposablesResult(
                anchor_objects=[],
                dimensions=sorted(
                    name
                    for name, dim in self.model.dimensions.items()
                    if self._has_common_root({dim.view} | dimension_requirements(self.model, name))
                ),
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
                    if self._metric_legs_reachable(name, set())
                    and not self._metric_blocked(name, set())
                ),
            )

        spine = dim_objects  # grouping dimensions shared across all legs
        leg_facts = measure_objects  # facts of measures already in the query
        waypoints = (waypoints or set()) & spine
        # What a CFL leg has to reach: a waypoint it cannot reach leaves its
        # dimension NULL on that leg.
        leg_spine = spine - waypoints

        # Dimensions: a new dimension object must be groupable at the current
        # grain. With measures present it must be reachable from every existing
        # leg fact; without measures it must merely co-root with the spine.
        dimensions = [
            name
            for name, dim in self.model.dimensions.items()
            if self._dimension_composable(
                dim.view,
                spine,
                leg_facts,
                self.model.dimension_join_objects(name),
                dimension_waypoint(self.model, name),
            )
        ]

        measures: list[str] = []
        cfl_measures: list[str] = []
        for name in self.model.effective_measures:
            sources = measure_source_objects(self.model, name)
            # Planned with the query's dimensions; a fact of a measure already
            # selected is a leg of its own and need not share a root with it.
            if not self._join_requirements_reachable(
                measure_join_requirements(self.model, name), leg_spine | sources
            ):
                continue
            padded = self._padded_waypoints(sources, leg_facts, waypoints)
            if self._measure_blocked(name, anchor - padded):
                continue
            status = self._measure_status(
                sources, anchor, spine, measure_join_requirements(self.model, name), padded
            )
            if status == "direct":
                measures.append(name)
            elif status == "cfl":
                cfl_measures.append(name)

        metrics: list[str] = []
        cfl_metrics: list[str] = []
        for name in self.model.metrics:
            if not self._metric_legs_reachable(name, leg_spine, waypoints):
                continue
            # Classified by what this query's plan reads for it; a stage of its
            # own (a reaggregate's first) was checked leg by leg above.
            plan_sources = metric_plan_sources(self.model, name)
            padded = self._padded_waypoints(plan_sources, leg_facts, waypoints)
            if self._metric_blocked(name, anchor - padded):
                continue
            status = self._measure_status(
                plan_sources, anchor, spine, metric_join_requirements(self.model, name), padded
            )
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

    @staticmethod
    def _padded_waypoints(
        plan_sources: set[str], leg_facts: set[str], waypoints: set[str]
    ) -> set[str]:
        """The waypoints a candidate's query may leave unreached.

        Over more than one fact (*plan_sources*, the facts the query's own plan
        reads for the candidate, and *leg_facts*) the query is planned as CFL
        legs, and a leg that cannot reach a waypoint projects NULL for its
        dimension and does not join it. A single-fact query is a star, which
        joins every one.
        """
        return waypoints if len(plan_sources | leg_facts) > 1 else set()

    def _metric_legs_reachable(
        self, name: str, spine: set[str], waypoints: set[str] | None = None
    ) -> bool:
        """``_join_requirements_reachable`` for each leg of a metric on its own.

        Asked of the metric's sources together, a metric over two facts with a
        requirement on either needed one root over both, though each fact is
        planned as a leg of its own. Each leg is planned with the query's
        dimensions (*spine*, *waypoints* left out), not with the facts of
        measures already selected, which are legs of their own.

        A reaggregate first stage over a single fact is a star query of its own,
        whatever else the outer query reads, so a leg it reads needs that
        stage's waypoints too: the query's and those of its ``per`` dimensions.
        Each stage is judged by its own facts; one over several is CFL and pads
        them, which an inner stage's extra fact does not do for an outer one.
        """
        for leg in metric_legs(self.model, name):
            if not self._join_requirements_reachable(set(leg.required), spine | leg.sources):
                return False
            for facts, stage_waypoints in leg.stages:
                single_fact_star = len(facts) <= 1 and (not leg.sources or leg.sources & facts)
                needed_waypoints = (waypoints or set()) | stage_waypoints
                if single_fact_star and needed_waypoints:
                    needed = spine | needed_waypoints | leg.sources | leg.required
                    if not self._has_common_root(needed):
                        return False
        return True

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

        # Reaching any one of the objects this measure forces into the query
        # means unnesting, which replicates the others. That is the case the
        # rewrite cannot express - it has no way to know which grain to
        # deduplicate on - so the compiler refuses, and ACR must not advertise
        # what it refuses.
        #
        # ``referenced`` counts, not only ``sources``. A ``withinGroup`` sort key
        # and a measure ``filter`` read no value and still force their object
        # into the query, so one sitting behind a containment edge replicates the
        # measure's own source exactly as a value column would - and judging on
        # the value columns alone said this measure was untouched.
        if self._needs_unnest_to_connect(sources | referenced):
            return "refused"

        # Everything that forces a join: the callers' drivers, plus whatever
        # this measure's own clauses drag in.
        forcing = drivers | outside
        replicated = {
            obj for obj in sources if any(obj in self.graph.descendants(d) for d in forcing)
        }
        # An unnest replicates the other way round: the array multiplies the row
        # that *contains* it, so a driver nested inside a source replicates that
        # source rather than the reverse. Reading the descendant relation alone
        # missed it entirely and advertised a parent-side measure as untouched.
        unnesting = {d for d in forcing if self.model.unnest_root(d) != d}
        replicated |= {obj for obj in sources if any(self._nested_under(d, obj) for d in unnesting)}
        if sources != replicated:
            # Not replicated here, so the pass never runs on it.
            return None

        # Several replicated sources, or a clause reaching outside the one being
        # deduplicated: the rewrite cannot express either and raises.
        if len(sources) > 1 or outside:
            return "refused"

        # Deduplicating needs one row per row of the source, and only a declared
        # key says which rows those are. Reachable only through an unnest: an
        # ordinary join always names the columns it matches on, which serve as
        # the identity where no primaryKey is declared.
        source = next(iter(sources))
        if any(self._nested_under(d, source) for d in unnesting) and not self._has_key(source):
            return "refused"
        return "dedup"

    def _nested_under(self, name: str, ancestor: str) -> bool:
        """Whether *name* is a nested object contained, at any depth, in *ancestor*."""
        obj = self.model.data_objects.get(name)
        while obj is not None and obj.nested_in is not None:
            if obj.nested_in.data_object == ancestor:
                return True
            obj = self.model.data_objects.get(obj.nested_in.data_object)
        return False

    def _has_key(self, name: str) -> bool:
        obj = self.model.data_objects.get(name)
        return obj is not None and any(col.primary_key for col in obj.columns.values())

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
        that metric's wrapper instead. A reaggregate reference is walked through
        too: its measure is planned as a query of its own, which deduplicates it
        there, so no wrapper of this query has to read a deduplicated value.
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
                if referenced.type in (MetricType.DERIVED, MetricType.REAGGREGATE):
                    pending.append(ref)
                else:
                    behind |= metric_leaf_measures(self.model, ref)
        return behind

    def _dimension_composable(
        self,
        obj: str,
        spine: set[str],
        leg_facts: set[str],
        reads: set[str] | None = None,
        waypoint: str | None = None,
    ) -> bool:
        # A computed column reads other data objects, and the planner joins
        # them wherever the dimension is projected — so they are as much a
        # requirement as the dimension's own object. So is a ``via`` object,
        # except on a CFL leg (independent leg facts), which projects NULL for
        # the dimension when it cannot reach it.
        needed = {obj} | (reads or set())
        if waypoint and len(leg_facts) <= 1:
            needed.add(waypoint)
        if leg_facts:
            # Must be groupable across every existing measure leg.
            return all(self._reaches_all(fact, needed) for fact in leg_facts)
        # No measures yet: the new dimension must share a root with the spine.
        return self._has_common_root(spine | needed)

    def _measure_status(
        self,
        source_objects: set[str],
        anchor: set[str],
        spine: set[str],
        requirements: set[str] | None = None,
        padded: set[str] | None = None,
    ) -> str | None:
        """Classify a measure/metric as 'direct', 'cfl', or None (incompatible).

        *requirements* are objects the measure needs **joined** without reading a
        value from - a ``withinGroup`` sort key, a measure ``filter``. They bear
        on whether a leg can carry it exactly as its value columns do, since the
        leg has to join them all the same.

        *padded* are the waypoints a CFL leg need not reach
        (:meth:`_padded_waypoints`).
        """
        if not source_objects:
            # No resolvable source (e.g. COUNT(*)-style): always combinable.
            return "direct"
        # Direct: the whole query stays single-fact (a common root covers all).
        if self._has_common_root(anchor | source_objects):
            return "direct"
        # A leg is a star built out of tables, so it cannot carry a measure that
        # needs an unnest to be computed at all - whether the object *is* nested
        # or merely sits behind one. ``allowFanOut`` does not rescue either: the
        # leg cannot reach the object at all, rather than reaching it too often.
        needed = source_objects | (requirements or set())
        if self._needs_unnest_to_connect(needed) or any(
            (obj := self.model.data_objects.get(name)) is not None and obj.is_nested
            for name in needed
        ):
            return None
        # CFL: each source fact independently reaches the current grain, so it
        # can join as a separate UNION ALL leg. With no grain yet, independent
        # facts still combine as grand-total legs.
        spine = spine - (padded or set())
        if not spine or all(self._reaches_all(fact, spine) for fact in source_objects):
            return "cfl"
        return None


def resolve_composables_for_query(model: SemanticModel, query: QueryObject) -> ComposablesResult:
    """Convenience: resolve composables for a whole in-progress query."""
    resolver = ComposabilityResolver(model, query.use_path_names or None)
    dim_objects, measure_objects = resolver.objects_from_query(query)
    names = [
        name
        for entry in query.select.dimensions
        for name in (entry.coalesce if isinstance(entry, CoalesceDimension) else [entry])
    ]
    return resolver.resolve(dim_objects, measure_objects, resolver.waypoints(names))


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
    dimension_names: list[str] = []
    others: set[str] = set()
    for name in anchors:
        if anchor_type in (None, "dimension") and name in model.dimensions:
            dimension_names.append(name)
        else:
            others |= resolver.objects_from_anchor_name(name, anchor_type)[0]
    return resolver.resolve(
        dim_objects, measure_objects, resolver.waypoints(dimension_names, others)
    )
