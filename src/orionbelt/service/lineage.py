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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from orionbelt.compiler.pipeline import CompilationResult
from orionbelt.models.expressions import find_placeholders, find_qualified_refs
from orionbelt.models.query import (
    CoalesceDimension,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
    Subquery,
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
KINDS = ("data_object", "column", "dimension", "measure", "metric", "rule", "union", "query")

_SHAPES = {
    "data_object": ('[("', '")]'),
    "column": ('["', '"]'),
    "dimension": ('(["', '"])'),
    "measure": ('{{"', '"}}'),
    "metric": ('[["', '"]]'),
    "rule": ('>"', '"]'),
    "union": ('{"', '"}'),
    "query": ('(("', '"))'),
}

_STYLES = {
    "data_object": "fill:#e8eef7,stroke:#5b7db1,color:#1f2d45",
    "column": "fill:#f4f6f8,stroke:#8a96a3,color:#2b333b",
    "dimension": "fill:#e6f4ea,stroke:#4a9460,color:#1d3b26",
    "measure": "fill:#fff4e0,stroke:#c98a1b,color:#4a3208",
    "metric": "fill:#fde8e8,stroke:#c0504d,color:#4a1d1c",
    "rule": "fill:#efe7fb,stroke:#7e57c2,color:#2e1f4a",
    "union": "fill:#fff8d6,stroke:#b8962e,color:#4a3b0c",
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
    """``source`` feeds ``target``; ``label`` says how, when it is not plain use.

    ``path_name`` names the secondary join a ``join on`` edge follows.
    """

    source: str
    target: str
    label: str | None = None
    path_name: str | None = None


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
            text = edge.label or ""
            if text and edge.path_name:
                text = f"{text} (path {edge.path_name})"
            arrow = f'-->|"{_escape(text)}"|' if text else "-->"
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
        if node.kind in ("query", "union"):
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
            join = artefact_uri(
                model_id,
                "join",
                by_id[edge.target].name,
                by_id[edge.source].name,
                path_name=edge.path_name,
            )
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

    def query(self, query: QueryObject, plan: QueryPlanFacts | None = None) -> Lineage:
        """Lineage of *query*, with the planner's joins and legs from *plan*."""
        return self._build(lambda: self._query(query, plan or QueryPlanFacts()))

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

    def _edge(
        self, source: str, target: str, label: str | None = None, path_name: str | None = None
    ) -> None:
        edge = LineageEdge(source, target, label, path_name)
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
        """A dimension (optionally ``Name:grain``), measure or metric by name.

        The exact name wins, so a measure called ``Sales: Retail`` is that
        measure; a ``:grain`` suffix is only read off a dimension name.
        """
        if name in self.model.dimensions:
            return self._dimension(name)
        if name in self._measures:
            return self._measure(name)
        if name in self.model.metrics:
            return self._metric(name)
        base, _, _grain = name.rpartition(":")
        if base in self.model.dimensions:
            return self._dimension(base)
        return None

    def _qualified_column(self, ref: str) -> str | None:
        """A ``DataObject.Column`` reference, as raw mode and filters read it."""
        obj_name, _, col_name = ref.partition(".")
        obj_name, col_name = obj_name.strip(), col_name.strip()
        obj = self.model.data_objects.get(obj_name)
        if obj is None or col_name not in obj.columns:
            return None
        return self._column(obj_name, col_name)

    def _filter_field(self, name: str, *, having: bool) -> str | None:
        """A query filter's field, in the compiler's order (``filter_resolution``).

        A dimension, then for ``having`` a measure or metric, then a qualified
        column.
        """
        if name in self.model.dimensions:
            return self._dimension(name)
        if having and (name in self._measures or name in self.model.metrics):
            return self._field(name)
        return self._qualified_column(name)

    def _subquery_field(self, name: str, target: str) -> str | None:
        """An ``exists`` subquery filter's field, in the compiler's order.

        A column of the subquery's own data object shadows a same-named
        dimension; then a dimension's column; then a qualified column.
        """
        target_obj = self.model.data_objects.get(target)
        if target_obj is not None and name in target_obj.columns:
            return self._column(target, name)
        dim = self.model.dimensions.get(name)
        if dim is not None and dim.column:
            return self._column(dim.view, dim.column)
        return self._qualified_column(name)

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

    def _query(self, query: QueryObject, plan: QueryPlanFacts) -> str:
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
        for raw in query.select.fields:
            # Raw mode reads the column, whatever else shares its spelling.
            source = self._qualified_column(raw)
            if source:
                self._edge(source, node, "field")
        for label, items in (("where", query.where), ("having", query.having)):
            for item in _query_filters(list(items)):
                source = self._filter_field(item.field, having=label == "having")
                if source:
                    self._edge(source, node, label)
                if item.subquery is not None:
                    self._subquery(item.subquery, str(item.op), node)
        for order in query.order_by:
            for source in self._order_sources(query, order.field):
                self._edge(source, node, "order")
        for from_object, to_object, columns, path_name in plan.joins:
            self._edge(
                self._table(from_object),
                self._table(to_object),
                f"join on {columns}",
                path_name,
            )
        if plan.legs:
            self._union(plan.legs)
        return node

    def _order_sources(self, query: QueryObject, name: str) -> list[str]:
        """What an ``orderBy`` field sorts by, resolved as the compiler does.

        Only against the query's own SELECT (``filter_resolution``): a coalesce
        alias, a selected dimension, a selected measure or metric, a selected
        raw field, or a 1-based SELECT position. A raw ``Orders.Amount`` is the
        column even when a measure shares that spelling.
        """
        dims: list[tuple[str, list[str]]] = []
        for dim in query.select.dimensions:
            if isinstance(dim, CoalesceDimension):
                dims.append((dim.alias, list(dim.coalesce)))
            else:
                dims.append((dim, [dim]))
        selected: list[list[str]] = [
            *(sources for _, sources in dims),
            *([m] for m in query.select.measures),
        ]
        for label, sources in dims:
            # A dimension selected as ``Name:grain`` sorts by ``Name`` too.
            if name in (label, label.rpartition(":")[0]):
                return self._nodes(self._field(s) for s in sources)
        if name in query.select.measures:
            return self._nodes([self._field(name)])
        if name in query.select.fields:
            return self._nodes([self._qualified_column(name)])
        if name.isdigit():
            position = int(name) - 1
            if 0 <= position < len(selected):
                return self._nodes(self._field(s) for s in selected[position])
            fields = list(query.select.fields)
            if 0 <= position - len(selected) < len(fields):
                return self._nodes([self._qualified_column(fields[position - len(selected)])])
        return []

    @staticmethod
    def _nodes(ids: Iterable[str | None]) -> list[str]:
        return [i for i in ids if i]

    def _subquery(self, sub: Subquery, op: str, node: str) -> None:
        """An ``exists``/``nonexists`` filter reads its subquery's data object and filters."""
        self._edge(self._table(sub.data_object), node, op)
        for item in sub.filter:
            source = self._subquery_field(item.field, sub.data_object)
            if source:
                self._edge(source, node, op)

    def _union(self, legs: list[tuple[str, list[str]]]) -> None:
        """Route each leg's measures through the ``UNION ALL`` that combines the legs.

        A multi-fact query computes each fact's measures in its own leg and
        stacks the legs with ``UNION ALL``; the metrics and the query read the
        measures from there, so their edges from a leg's measure now start at
        the union node.
        """
        union = self._node("union", "UNION ALL", f"{len(legs)} legs")
        leg_of: dict[str, str] = {}
        for source, measures in legs:
            for measure in measures:
                if measure in self._measures:
                    leg_of[self._measure(measure)] = source
        rerouted: list[LineageEdge] = []
        for edge in self._lineage.edges:
            target_kind = edge.target.split(":", 1)[0]
            if edge.source in leg_of and target_kind in ("metric", "query"):
                rerouted.append(replace(edge, source=union))
            else:
                rerouted.append(edge)
        self._lineage.edges = []
        self._seen_edges.clear()
        for edge in rerouted:
            self._edge(edge.source, edge.target, edge.label, edge.path_name)
        for measure_node, source in leg_of.items():
            self._edge(measure_node, union, f"leg {source}")


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


def _query_filters(items: list[QueryFilter | QueryFilterGroup]) -> list[QueryFilter]:
    """The leaf filters of a query filter list, groups flattened."""
    leaves: list[QueryFilter] = []
    for item in items:
        if isinstance(item, QueryFilterGroup):
            leaves.extend(_query_filters(list(item.filters)))
        else:
            leaves.append(item)
    return leaves


@dataclass
class QueryPlanFacts:
    """What the planner decided for a query that lineage shows.

    ``joins`` are ``(from, to, columns, path_name)`` steps, ``path_name`` set for
    a secondary join; ``legs`` are ``(fact, measures)`` for a multi-fact query,
    whose legs a ``UNION ALL`` combines.
    """

    joins: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    legs: list[tuple[str, list[str]]] = field(default_factory=list)


def query_plan_facts(result: CompilationResult) -> QueryPlanFacts:
    """The joins, and for a multi-fact query the legs, of a compiled query."""
    facts = QueryPlanFacts()
    if result.explain is None:
        return facts
    steps = list(result.explain.joins)
    for leg in result.explain.cfl_legs:
        steps.extend(leg.join_steps)
        facts.legs.append((leg.measure_source, list(leg.measures)))
    for step in steps:
        # Draw each join as the model declares it, owner to target, with
        # declared object names rather than role aliases, so the Turtle names
        # the declared join's IRI.
        owner = step.declared_from or step.from_object
        target = step.declared_to or step.to_object
        join = (owner, target, ", ".join(step.join_columns), step.path_name)
        if join not in facts.joins:
            facts.joins.append(join)
    return facts
