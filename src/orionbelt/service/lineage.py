"""Lineage of a model artefact or a query, down to the tables it reads.

A lineage is a directed graph whose edges point from a source to what is built
from it: a table's column feeds a measure, the measure feeds a metric, the
metric feeds the query. It follows references transitively, so a metric's
lineage reaches the columns and tables under its measures.

What an artefact depends on, by kind:

* dimension - its column, and the ``via`` data object it is read through;
* measure - its columns or expression columns, its filter columns (``filter``),
  its ``withinGroup`` column, the dimensions of a grain override (``grain``),
  its ``anchor`` object; a synthesized count reads its data object's rows;
* metric - the measures and metrics its expression names; for cumulative and
  window metrics, the measure plus the time and partition dimensions;
* rule - the dimensions and measures its condition reads, the rules it
  references, and its grain dimensions;
* query - its dimensions, measures and metrics, its filter and order fields,
  and the joins the planner chose.

A column that is a computed ``expression`` depends on the columns it reads.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from orionbelt.compiler.pipeline import CompilationResult
from orionbelt.models.expressions import find_placeholders, find_qualified_refs
from orionbelt.models.query import (
    CoalesceDimension,
    DimensionRef,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
)
from orionbelt.models.semantic import (
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    MetricType,
    RuleCondition,
    SemanticModel,
)

_MEASURE_REF = re.compile(r"\{\[([^\]]+)\]\}")

#: Node kinds, in the order the Mermaid legend lists them.
KINDS = ("data_object", "column", "dimension", "measure", "metric", "rule", "query")

_SHAPES = {
    "data_object": ('[("', '")]'),
    "column": ('["', '"]'),
    "dimension": ('(["', '"])'),
    "measure": ('{{"', '"}}'),
    "metric": ('[["', '"]]'),
    "rule": ('>"', '"]'),
    "query": ('(("', '"))'),
}

_STYLES = {
    "data_object": "fill:#e8eef7,stroke:#5b7db1,color:#1f2d45",
    "column": "fill:#f4f6f8,stroke:#8a96a3,color:#2b333b",
    "dimension": "fill:#e6f4ea,stroke:#4a9460,color:#1d3b26",
    "measure": "fill:#fff4e0,stroke:#c98a1b,color:#4a3208",
    "metric": "fill:#fde8e8,stroke:#c0504d,color:#4a1d1c",
    "rule": "fill:#efe7fb,stroke:#7e57c2,color:#2e1f4a",
    "query": "fill:#e0f2f1,stroke:#26877a,color:#123d38",
}


class LineageError(Exception):
    """The requested artefact does not exist, or the query cannot be planned."""


@dataclass(frozen=True)
class LineageNode:
    """One artefact in a lineage graph."""

    id: str
    kind: str
    name: str
    detail: str | None = None


@dataclass(frozen=True)
class LineageEdge:
    """``source`` feeds ``target``; ``label`` says how, when it is not plain use."""

    source: str
    target: str
    label: str | None = None


@dataclass
class Lineage:
    """A lineage graph rooted at one artefact or query."""

    root: str
    nodes: list[LineageNode] = field(default_factory=list)
    edges: list[LineageEdge] = field(default_factory=list)

    def to_mermaid(self) -> str:
        """Render as a left-to-right Mermaid flowchart, sources on the left."""
        ids = {node.id: f"n{i}" for i, node in enumerate(self.nodes)}
        lines = ["flowchart LR"]
        for node in self.nodes:
            left, right = _SHAPES[node.kind]
            label = _escape(node.name)
            if node.detail:
                label += f"<br/>{_escape(node.detail)}"
            lines.append(f"    {ids[node.id]}{left}{label}{right}")
        for edge in self.edges:
            arrow = f"-- {_escape(edge.label)} -->" if edge.label else "-->"
            lines.append(f"    {ids[edge.source]} {arrow} {ids[edge.target]}")
        used = {node.kind for node in self.nodes}
        for kind in KINDS:
            if kind in used:
                lines.append(f"    classDef {kind} {_STYLES[kind]}")
        for kind in KINDS:
            members = [ids[n.id] for n in self.nodes if n.kind == kind]
            if members:
                lines.append(f"    class {','.join(members)} {kind}")
        root = ids.get(self.root)
        if root:
            lines.append(f"    style {root} stroke-width:3px")
        return "\n".join(lines)


_METRIC_CLASSES = {
    "cumulative": "CumulativeMetric",
    "period_over_period": "PeriodOverPeriodMetric",
    "window": "WindowMetric",
}


def to_turtle(lineage: Lineage, model_id: str) -> str:
    """Render *lineage* as Turtle over the OBSL graph's IRIs.

    Each node keeps the IRI and class it has in the model's OBSL-Core graph, so
    the two graphs merge. An edge becomes ``target prov:wasDerivedFrom source``
    (W3C PROV); a query is a ``prov:Entity`` blank node derived from the joins
    the planner used. Edge labels such as ``filter`` stay in the JSON and
    Mermaid forms.
    """
    from rdflib import RDF, RDFS, BNode, Graph, Literal, Namespace
    from rdflib.term import Node

    from orionbelt.obsl.exporter import OBSL, artefact_uri

    prov = Namespace("http://www.w3.org/ns/prov#")
    graph = Graph()
    graph.bind("obsl", OBSL)
    graph.bind("prov", prov)
    by_id = {n.id: n for n in lineage.nodes}
    iris: dict[str, Node] = {}
    for node in lineage.nodes:
        if node.kind == "query":
            iri: Node = BNode()
            graph.add((iri, RDF.type, prov.Entity))
        else:
            data_object = node.detail or "" if node.kind == "column" else ""
            iri = artefact_uri(model_id, node.kind, node.name, data_object)
            cls = {
                "data_object": "DataObject",
                "column": "Column",
                "dimension": "Dimension",
                "measure": "Measure",
                "metric": _METRIC_CLASSES.get(node.detail or "", "Metric"),
                "rule": "Rule",
            }[node.kind]
            graph.add((iri, RDF.type, OBSL[cls]))
        graph.add((iri, RDFS.label, Literal(node.name)))
        iris[node.id] = iri
    root = iris.get(lineage.root)
    for edge in lineage.edges:
        if edge.label and edge.label.startswith("join on ") and root is not None:
            join = artefact_uri(model_id, "join", by_id[edge.target].name, by_id[edge.source].name)
            graph.add((join, RDF.type, OBSL.Join))
            graph.add((root, prov.wasDerivedFrom, join))
        else:
            graph.add((iris[edge.target], prov.wasDerivedFrom, iris[edge.source]))
    return graph.serialize(format="turtle")


def _escape(text: str) -> str:
    """Make *text* safe inside a quoted Mermaid label."""
    return (
        text.replace("&", "&amp;").replace('"', "#quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


class LineageBuilder:
    """Build lineage graphs over one model."""

    def __init__(self, model: SemanticModel) -> None:
        self.model = model
        self._measures = model.effective_measures
        self._lineage = Lineage(root="")
        self._seen_nodes: set[str] = set()
        self._seen_edges: set[LineageEdge] = set()
        self._expanded: set[str] = set()

    # ── entry points ──────────────────────────────────────────────────

    def dimension(self, name: str) -> Lineage:
        if name not in self.model.dimensions:
            raise LineageError(f"Dimension '{name}' not found")
        return self._build(lambda: self._dimension(name))

    def measure(self, name: str) -> Lineage:
        if name not in self._measures:
            raise LineageError(f"Measure '{name}' not found")
        return self._build(lambda: self._measure(name))

    def metric(self, name: str) -> Lineage:
        if name not in self.model.metrics:
            raise LineageError(f"Metric '{name}' not found")
        return self._build(lambda: self._metric(name))

    def rule(self, name: str) -> Lineage:
        if name not in self.model.rules:
            raise LineageError(f"Rule '{name}' not found")
        return self._build(lambda: self._rule(name))

    def query(self, query: QueryObject, joins: list[tuple[str, str, str]] | None = None) -> Lineage:
        """Lineage of *query*; *joins* are the planner's ``(from, to, columns)`` steps."""
        return self._build(lambda: self._query(query, joins or []))

    # ── graph plumbing ────────────────────────────────────────────────

    def _build(self, expand: Callable[[], str]) -> Lineage:
        self._lineage = Lineage(root="")
        self._seen_nodes.clear()
        self._seen_edges.clear()
        self._expanded.clear()
        self._lineage.root = expand()
        return self._lineage

    def _node(self, kind: str, name: str, detail: str | None = None, key: str | None = None) -> str:
        node_id = f"{kind}:{key or name}"
        if node_id not in self._seen_nodes:
            self._seen_nodes.add(node_id)
            self._lineage.nodes.append(LineageNode(node_id, kind, name, detail))
        return node_id

    def _edge(self, source: str, target: str, label: str | None = None) -> None:
        edge = LineageEdge(source, target, label)
        if source != target and edge not in self._seen_edges:
            self._seen_edges.add(edge)
            self._lineage.edges.append(edge)

    def _once(self, node_id: str) -> bool:
        """True the first time *node_id* is expanded; guards shared sub-graphs."""
        if node_id in self._expanded:
            return False
        self._expanded.add(node_id)
        return True

    # ── tables and columns ────────────────────────────────────────────

    def _table(self, data_object: str) -> str:
        obj = self.model.data_objects.get(data_object)
        detail = None
        if obj is not None and obj.code:
            detail = ".".join(p for p in (obj.database, obj.schema_name, obj.code) if p)
        return self._node("data_object", data_object, detail)

    def _column(self, data_object: str, column: str) -> str:
        node = self._node("column", column, data_object, key=f"{data_object}.{column}")
        if not self._once(node):
            return node
        self._edge(self._table(data_object), node)
        obj = self.model.data_objects.get(data_object)
        col = obj.columns.get(column) if obj is not None else None
        if col is not None and col.expression:
            for sibling in find_placeholders(col.expression):
                self._edge(self._column(data_object, sibling), node, "expression")
            for other_object, other_column in find_qualified_refs(col.expression):
                self._edge(self._column(other_object, other_column), node, "expression")
        return node

    # ── semantic artefacts ────────────────────────────────────────────

    def _field(self, name: str) -> str | None:
        """A dimension, measure or metric by name; None when the name is none of them."""
        name = DimensionRef.parse(name).name if ":" in name else name
        if name in self.model.dimensions:
            return self._dimension(name)
        if name in self._measures:
            return self._measure(name)
        if name in self.model.metrics:
            return self._metric(name)
        return None

    def _dimension(self, name: str) -> str:
        dim = self.model.dimensions[name]
        detail = dim.time_grain.value if dim.time_grain else None
        node = self._node("dimension", name, detail)
        if self._once(node):
            if dim.column:
                self._edge(self._column(dim.view, dim.column), node)
            if dim.via:
                self._edge(self._table(dim.via), node, "via")
        return node

    def _measure(self, name: str) -> str:
        m = self._measures[name]
        node = self._node("measure", name, str(m.aggregation))
        if not self._once(node):
            return node
        for ref in m.columns:
            if ref.view and ref.column:
                self._edge(self._column(ref.view, ref.column), node)
            elif ref.view:
                self._edge(self._table(ref.view), node, "rows")
        if m.expression:
            for data_object, column in find_qualified_refs(m.expression):
                self._edge(self._column(data_object, column), node)
        for column_ref in self._filter_columns(m.filters):
            self._edge(column_ref, node, "filter")
        if m.within_group and m.within_group.column.view and m.within_group.column.column:
            wg = m.within_group.column
            self._edge(self._column(str(wg.view), str(wg.column)), node, "order")
        if m.grain:
            for dim in (*m.grain.include, *m.grain.exclude, *m.grain.keep_only):
                if dim in self.model.dimensions:
                    self._edge(self._dimension(dim), node, "grain")
        if m.anchor:
            self._edge(self._table(m.anchor), node, "anchor")
        return node

    def _filter_columns(self, items: list[MeasureFilterItem]) -> list[str]:
        found: list[str] = []
        for item in items:
            if isinstance(item, MeasureFilterGroup):
                found.extend(self._filter_columns(list(item.filters)))
            elif isinstance(item, MeasureFilter) and item.column:
                ref = item.column
                if ref.view and ref.column:
                    found.append(self._column(ref.view, ref.column))
        return found

    def _metric(self, name: str) -> str:
        met = self.model.metrics[name]
        node = self._node("metric", name, met.type.value if met.type else None)
        if not self._once(node):
            return node
        refs = _MEASURE_REF.findall(met.expression or "")
        if met.measure:
            refs.append(met.measure)
        for ref in refs:
            source = self._field(ref)
            if source:
                self._edge(source, node)
        time_dims: list[str] = []
        if met.type in (MetricType.CUMULATIVE, MetricType.WINDOW) and met.time_dimension:
            time_dims.append(met.time_dimension)
        if met.period_over_period:
            time_dims.append(met.period_over_period.time_dimension)
        for dim in time_dims:
            if dim in self.model.dimensions:
                self._edge(self._dimension(dim), node, "time")
        for dim in met.partition_by:
            if dim in self.model.dimensions:
                self._edge(self._dimension(dim), node, "partition")
        return node

    def _rule(self, name: str) -> str:
        rule = self.model.rules[name]
        node = self._node("rule", name, rule.type.value)
        if not self._once(node):
            return node
        fields, rules = _condition_refs(rule.condition)
        for field_name in fields:
            source = self._field(field_name)
            if source:
                self._edge(source, node, "condition")
        for other in rules:
            if other in self.model.rules:
                self._edge(self._rule(other), node, "rule")
        for dim in rule.grain:
            if dim in self.model.dimensions:
                self._edge(self._dimension(dim), node, "grain")
        return node

    def _query(self, query: QueryObject, joins: list[tuple[str, str, str]]) -> str:
        node = self._node("query", "Query")
        for dim in query.select.dimensions:
            names = dim.coalesce if isinstance(dim, CoalesceDimension) else [dim]
            for dim_name in names:
                source = self._field(dim_name)
                if source:
                    self._edge(source, node)
        for measure in query.select.measures:
            source = self._field(measure)
            if source:
                self._edge(source, node)
        for label, items in (("where", query.where), ("having", query.having)):
            for field_name in _query_filter_fields(list(items)):
                source = self._field(field_name)
                if source:
                    self._edge(source, node, label)
        for order in query.order_by:
            source = self._field(order.field)
            if source:
                self._edge(source, node, "order")
        for from_object, to_object, columns in joins:
            self._edge(self._table(from_object), self._table(to_object), f"join on {columns}")
        return node


def _condition_refs(condition: RuleCondition) -> tuple[list[str], list[str]]:
    """The fields and the rules a rule condition names directly, in order."""
    fields: list[str] = []
    rules: list[str] = []

    def walk(c: RuleCondition) -> None:
        if c.field:
            fields.append(c.field)
        if c.rule:
            rules.append(c.rule)
        if c.not_ is not None:
            walk(c.not_)
        for child in (c.all_ or []) + (c.any_ or []):
            walk(child)

    walk(condition)
    return fields, rules


def _query_filter_fields(items: list[QueryFilter | QueryFilterGroup]) -> list[str]:
    fields: list[str] = []
    for item in items:
        if isinstance(item, QueryFilterGroup):
            fields.extend(_query_filter_fields(list(item.filters)))
        else:
            fields.append(item.field)
    return fields


def query_joins(result: CompilationResult) -> list[tuple[str, str, str]]:
    """The ``(from, to, columns)`` join steps the planner chose for a compiled query."""
    if result.explain is None:
        return []
    steps = list(result.explain.joins)
    for leg in result.explain.cfl_legs:
        steps.extend(leg.join_steps)
    joins: list[tuple[str, str, str]] = []
    for step in steps:
        join = (step.from_object, step.to_object, ", ".join(step.join_columns))
        if join not in joins:
            joins.append(join)
    return joins
