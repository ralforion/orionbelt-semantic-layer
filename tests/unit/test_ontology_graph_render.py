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
    still link to Orders via referencesColumn (regression for the orphaned node
    and for the SHACL-valid expression-dependency predicate)."""
    nodes, edges = _render()
    assert ("Orders", "referencesColumn") in _edges_from(nodes, edges, "Average Order Value")


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


# ---------------------------------------------------------------------------
# Computed columns
# ---------------------------------------------------------------------------

# Store.Zip Matches reads Address (cross-object), Store.Zip Differs reads only
# a sibling, Address.Zip 5 likewise. Only the first earns a node.
_COMPUTED_YAML = """\
version: 1.0

dataObjects:
  Store:
    code: STORE
    database: WH
    schema: PUBLIC
    columns:
      Store Zip: {code: S_ZIP, abstractType: string}
      Zip Matches:
        expression: "{Store Zip} = {[Address].[Zip 5]}"
        abstractType: boolean
      Zip Differs:
        expression: "NOT {Zip Matches}"
        abstractType: boolean

  Address:
    code: CUSTOMER_ADDRESS
    database: WH
    schema: PUBLIC
    columns:
      Address Key: {code: CA_ADDRESS_SK, abstractType: int}
      Zip: {code: CA_ZIP, abstractType: string}
      Zip 5:
        expression: "SUBSTRING({Zip}, 1, 5)"
        abstractType: string

  Sales:
    code: STORE_SALES
    database: WH
    schema: PUBLIC
    columns:
      Sold Store Key: {code: SS_STORE_SK, abstractType: int}
      Sold Address Key: {code: SS_ADDR_SK, abstractType: int}
      Amount: {code: SS_AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Sold Store Key]
        columnsTo: [Store Zip]
      - joinType: many-to-one
        joinTo: Address
        columnsFrom: [Sold Address Key]
        columnsTo: [Address Key]

dimensions:
  Zip Matches: {dataObject: Store, column: Zip Matches, resultType: boolean}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
"""


def _render_computed(**flags: bool) -> tuple[list[dict], list[dict]]:
    kwargs = dict(
        show_data_objects=True,
        show_dimensions=True,
        show_measures=True,
        show_metrics=True,
        show_joins=True,
        node_spacing=150,
    )
    kwargs.update(flags)
    return _parse(_generate_ontology_graph_html(_COMPUTED_YAML, **kwargs))


def test_only_cross_object_computed_columns_become_nodes() -> None:
    """A column reading another data object marks a join the planner has to
    make, so it earns a node. One reading only siblings says nothing the
    owning object does not already say, and stays collapsed."""
    nodes, _ = _render_computed()
    labels = {n["label"] for n in nodes}
    assert "Store.Zip Matches" in labels
    assert "Store.Zip Differs" not in labels
    assert "Address.Zip 5" not in labels
    # Qualified, so it cannot be confused with the same-named dimension.
    assert "Zip Matches" in labels


def test_computed_column_hangs_off_its_data_object() -> None:
    nodes, edges = _render_computed()
    assert ("Store.Zip Matches", "hasColumn") in _edges_from(nodes, edges, "Store")


def test_computed_column_links_to_what_it_reads() -> None:
    nodes, edges = _render_computed()
    assert ("Address", "referencesColumn") in _edges_from(nodes, edges, "Store.Zip Matches")


def test_sibling_reference_draws_no_edge() -> None:
    """``Zip Differs`` reads ``Zip Matches`` on its own object — already
    implied by both hanging off Store, so no second edge for it."""
    nodes, edges = _render_computed()
    assert ("Store.Zip Matches", "referencesColumn") not in _edges_from(nodes, edges, "Store")


def test_computed_columns_follow_the_data_object_filter() -> None:
    nodes, _ = _render_computed(show_data_objects=False)
    assert "Store.Zip Matches" not in {n["label"] for n in nodes}
