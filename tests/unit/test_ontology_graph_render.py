"""The UI Ontology Graph is rendered *from the OBSL ontology*.

`_generate_ontology_graph_html` exports the model to an RDF graph and maps its
individuals/predicates to vis-network nodes/edges, so the graph and the exported
ontology never drift. These tests assert that mapping on the sales fixture.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orionbelt.ui.rendering import _generate_ontology_graph_html

_MODEL_YAML = (
    Path(__file__).resolve().parents[1] / "fixtures" / "sales_model" / "model.yaml"
).read_text()


def _parse(html: str) -> tuple[list[dict], list[dict]]:
    """Pull the vis-network nodes + edges JSON out of the generated iframe HTML."""
    src = re.search(r'srcdoc="(.*)" ', html, re.S).group(1)
    src = src.replace("&quot;", '"').replace("&amp;", "&")
    nodes = json.loads(re.search(r"new vis\.DataSet\((\[.*?\])\);\nvar e", src, re.S).group(1))
    edges = json.loads(re.search(r"var e=new vis\.DataSet\((\[.*?\])\);", src, re.S).group(1))
    return nodes, edges


def _render(**flags: bool) -> tuple[list[dict], list[dict]]:
    kwargs = dict(
        show_data_objects=True,
        show_dimensions=True,
        show_measures=True,
        show_metrics=True,
        show_joins=True,
        node_spacing=150,
    )
    kwargs.update(flags)
    return _parse(_generate_ontology_graph_html(_MODEL_YAML, **kwargs))


def _edges_from(nodes: list[dict], edges: list[dict], label: str) -> list[tuple[str, str]]:
    by_id = {n["id"]: n["label"] for n in nodes}
    src_id = next(n["id"] for n in nodes if n["label"] == label)
    return [(by_id[e["to"]], e["label"]) for e in edges if e["from"] == src_id]


def test_nodes_cover_ontology_individuals() -> None:
    nodes, _ = _render()
    labels = {n["label"] for n in nodes}
    # data objects, a dimension, a declared measure, a metric, a synthesized count
    assert {"Customers", "Products", "Orders"} <= labels
    assert "Customer Country" in labels
    assert "Revenue" in labels
    assert "Orders Count" in labels  # synthesized row-count measure


def test_expression_measure_connects_to_its_object() -> None:
    """Average Order Value sources its columns via an expression only; it must
    still link to Orders (regression for the orphaned-node bug)."""
    nodes, edges = _render()
    assert ("Orders", "sourceColumn") in _edges_from(nodes, edges, "Average Order Value")


def test_synthesized_count_anchors_to_its_object() -> None:
    nodes, edges = _render()
    assert ("Orders", "anchor") in _edges_from(nodes, edges, "Orders Count")


def test_dimension_links_to_data_object() -> None:
    nodes, edges = _render()
    assert ("Customers", "dataObject") in _edges_from(nodes, edges, "Customer Country")


def test_metric_references_base_measure() -> None:
    nodes, edges = _render()
    labels = {e[1] for e in _edges_from(nodes, edges, "Running Revenue")}
    assert "baseMeasure" in labels or "referencesMeasure" in labels


def test_joins_render_as_object_edges() -> None:
    nodes, edges = _render()
    assert ("Customers", "many-to-one") in _edges_from(nodes, edges, "Orders")
    assert ("Products", "many-to-one") in _edges_from(nodes, edges, "Orders")


def test_filters_drop_types() -> None:
    """Unchecking Measures + Metrics removes those nodes and their edges."""
    nodes, _ = _render(show_measures=False, show_metrics=False)
    labels = {n["label"] for n in nodes}
    assert "Revenue" not in labels
    assert "Orders Count" not in labels
    assert "Customers" in labels  # data objects remain


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_empty_model_yaml(empty: str) -> None:
    assert "No model loaded" in _generate_ontology_graph_html(empty)
