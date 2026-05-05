"""Compute structural health of a loaded model's join graph.

See ``design/PLAN_agent_api_improvements.md`` §1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orionbelt.compiler.graph import JoinGraph
from orionbelt.models.semantic import Cardinality, SemanticModel


@dataclass
class FanTrapRisk:
    """Detected fan-trap risk between two facts sharing a dim."""

    tables: list[str]
    reason: str
    suggested_pattern: str = "composite_fact_layer"


@dataclass
class HealthSummary:
    """Structural health of a model's join graph."""

    status: str = "ok"
    data_objects: int = 0
    joins: int = 0
    orphan_data_objects: list[str] = field(default_factory=list)
    fan_trap_risks: list[FanTrapRisk] = field(default_factory=list)
    unreachable_dimensions: list[str] = field(default_factory=list)
    warnings_count: int = 0


def _qualified_table(model: SemanticModel, name: str) -> str:
    """Return the physical SQL table reference for a dataObject by label."""
    obj = model.data_objects.get(name)
    if obj is None:
        return name
    parts = [p for p in (obj.database, obj.schema_name, obj.code) if p]
    return ".".join(parts) if parts else name


def _detect_orphans(model: SemanticModel) -> list[str]:
    """DataObjects with no incoming or outgoing joins.

    A model with one dataObject is treated as intentional (no orphans).
    """
    if len(model.data_objects) <= 1:
        return []
    referenced: set[str] = set()
    for obj_name, obj in model.data_objects.items():
        for join in obj.joins:
            if join.join_to in model.data_objects:
                referenced.add(obj_name)
                referenced.add(join.join_to)
    return sorted(set(model.data_objects.keys()) - referenced)


def _detect_fan_trap_risks(model: SemanticModel) -> list[FanTrapRisk]:
    """Two fact-like dataObjects joined to the same dim via the same FK columns.

    Heuristic: dataObjects that have outgoing many-to-one joins to a shared
    target are 'fact-like'. When two such facts both join to the SAME target
    on the SAME local columns, a query touching both will produce row
    multiplication unless a Composite Fact Layer is used.
    """
    # target -> list[(source, columns_from_tuple)]
    inbound: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for obj_name, obj in model.data_objects.items():
        for join in obj.joins:
            if (
                join.join_type == Cardinality.MANY_TO_ONE
                and join.join_to in model.data_objects
                and not join.secondary
            ):
                key = tuple(join.columns_from)
                inbound.setdefault(join.join_to, []).append((obj_name, key))

    risks: list[FanTrapRisk] = []
    for target, sources in inbound.items():
        if len(sources) < 2:
            continue
        # Group by columns_from signature; matching columns means shared FK
        by_cols: dict[tuple[str, ...], list[str]] = {}
        for src, cols in sources:
            by_cols.setdefault(cols, []).append(src)
        for cols, srcs in by_cols.items():
            if len(srcs) < 2:
                continue
            tables = [_qualified_table(model, s) for s in sorted(srcs)]
            risks.append(
                FanTrapRisk(
                    tables=tables,
                    reason=(
                        f"both fact tables join to {_qualified_table(model, target)} "
                        f"via the same FK column(s) {list(cols)}"
                    ),
                    suggested_pattern="composite_fact_layer",
                )
            )
    return risks


def _detect_unreachable_dimensions(model: SemanticModel, graph: JoinGraph) -> list[str]:
    """Dimensions whose dataObject has no incoming many-to-one joins from any fact.

    A dimension is 'unreachable' when the dataObject it lives on cannot be
    joined-to from any fact dataObject. We treat any dataObject that has at
    least one outgoing many-to-one join as 'fact-like'.
    """
    fact_objects = {
        name
        for name, obj in model.data_objects.items()
        if any(j.join_type == Cardinality.MANY_TO_ONE for j in obj.joins)
    }
    if not fact_objects:
        return []

    # Compute set of dataObjects reachable from any fact via directed paths
    reachable: set[str] = set()
    for fact in fact_objects:
        reachable.add(fact)
        reachable |= graph.descendants(fact)

    unreachable: list[str] = []
    for dim_name, dim in model.dimensions.items():
        if dim.view not in reachable:
            unreachable.append(dim_name)
    return sorted(unreachable)


def compute_health(model: SemanticModel) -> HealthSummary:
    """Walk the join graph once and return structural health metrics."""
    graph = JoinGraph(model)

    join_count = sum(
        1
        for obj in model.data_objects.values()
        for j in obj.joins
        if j.join_to in model.data_objects and not j.secondary
    )

    orphans = _detect_orphans(model)
    fan_traps = _detect_fan_trap_risks(model)
    unreachable = _detect_unreachable_dimensions(model, graph)

    warnings_count = len(orphans) + len(fan_traps) + len(unreachable)
    status = "warnings" if warnings_count > 0 else "ok"

    return HealthSummary(
        status=status,
        data_objects=len(model.data_objects),
        joins=join_count,
        orphan_data_objects=orphans,
        fan_trap_risks=fan_traps,
        unreachable_dimensions=unreachable,
        warnings_count=warnings_count,
    )
