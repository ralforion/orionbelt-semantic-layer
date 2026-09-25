"""LineageBuilder: transitive graphs, planner joins, Mermaid and Turtle rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from rdflib import Graph

from orionbelt.models.query import QueryObject
from orionbelt.service.lineage import (
    Lineage,
    LineageBuilder,
    LineageError,
    LineageNode,
    query_joins,
    to_turtle,
)
from orionbelt.service.model_store import ModelStore

_COMMERCE = Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml"


@pytest.fixture(scope="module")
def loaded() -> tuple[ModelStore, str]:
    store = ModelStore()
    result = store.load_model(_COMMERCE.read_text(encoding="utf-8"))
    return store, result.model_id


def _edges(lineage: Lineage) -> set[tuple[str, str, str | None]]:
    return {(e.source, e.target, e.label) for e in lineage.edges}


def test_multi_fact_query_gets_every_legs_joins(loaded) -> None:
    store, model_id = loaded
    query = QueryObject.model_validate(
        yaml.safe_load(
            "select: {dimensions: [Product Name, Client Name], "
            "measures: [Total Sales, Total Returns]}"
        )
    )
    result = store.compile_query(model_id, query, "duckdb")
    assert result.explain is not None and result.explain.planner == "CFL"
    joins = query_joins(result)
    targets = {(f, t) for f, t, _ in joins}
    assert {("Sales", "Clients"), ("Sales", "Products"), ("Returns", "Sales")} <= targets
    lineage = LineageBuilder(store.get_model(model_id)).query(query, joins)
    assert any(
        s == "data_object:Returns"
        and t == "data_object:Sales"
        and (lbl or "").startswith("join on")
        for s, t, lbl in _edges(lineage)
    )


def test_shared_measure_appears_once(loaded) -> None:
    store, model_id = loaded
    lineage = LineageBuilder(store.get_model(model_id)).metric("Gross Margin")
    ids = [n.id for n in lineage.nodes]
    assert len(ids) == len(set(ids))
    assert len(lineage.edges) == len(set(lineage.edges))


def test_rule_on_rule(loaded) -> None:
    store, model_id = loaded
    lineage = LineageBuilder(store.get_model(model_id)).rule("Healthy Category")
    assert ("rule:High Return Rate", "rule:Healthy Category", "rule") in _edges(lineage)


def test_unknown_names(loaded) -> None:
    store, model_id = loaded
    builder = LineageBuilder(store.get_model(model_id))
    for build in (builder.dimension, builder.measure, builder.metric, builder.rule):
        with pytest.raises(LineageError):
            build("Nope")


def test_mermaid_escapes_labels() -> None:
    lineage = Lineage(
        root="measure:x",
        nodes=[LineageNode("measure:x", "measure", 'Say "hi" <b>', "sum")],
    )
    text = lineage.to_mermaid()
    assert "#quot;hi#quot; &lt;b&gt;" in text
    assert "style n0 stroke-width:3px" in text


def test_turtle_parses_and_types_nodes(loaded) -> None:
    store, model_id = loaded
    lineage = LineageBuilder(store.get_model(model_id)).rule("High Value Client")
    graph = Graph().parse(data=to_turtle(lineage, model_id), format="turtle")
    types = {str(o).rsplit("#", 1)[-1] for o in graph.objects() if "obsl#" in str(o)}
    assert {"Rule", "Measure", "Column", "DataObject", "Dimension"} <= types
