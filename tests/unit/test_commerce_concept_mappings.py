"""The commerce demo carries synthetic external concept mappings, and every
SPARQL example in the OBSL guide runs against its exported graph.

The demo model is what the public playground loads, so this is the canonical
showcase for the OBML -> RDF -> SPARQL path: the mappings must resolve, the
graph must carry them as SKOS triples, and the guide's ready-to-run queries
must actually run (and the mapping ones must find something).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rdflib import Graph
from rdflib.namespace import SKOS

from orionbelt.models.semantic import SemanticModel
from orionbelt.obsl.exporter import export_obsl
from orionbelt.obsl.sparql import execute_sparql
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.concept_index import ConceptMappingIndex

_REPO = Path(__file__).resolve().parents[2]
_MODEL = _REPO / "examples" / "orionbelt_1_commerce.yaml"
_GUIDE = _REPO / "docs" / "guide" / "obsl.md"
COMMERCE = "https://example.com/ontology/commerce/"


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, sm = TrackedLoader().load(_MODEL)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.errors == [], result.errors
    return model


@pytest.fixture(scope="module")
def graph(model: SemanticModel) -> Graph:
    return export_obsl(model, "orionbelt_1_commerce")


@pytest.fixture(scope="module")
def index(model: SemanticModel) -> ConceptMappingIndex:
    return ConceptMappingIndex(model, "orionbelt_1_commerce")


def test_demo_declares_its_prefixes(model: SemanticModel) -> None:
    assert model.ontology is not None
    assert set(model.ontology.prefixes) == {"commerce", "schema", "gr", "fibo"}


def test_high_value_objects_are_mapped(index: ConceptMappingIndex) -> None:
    mapped = {(link.object.type, link.object.name) for link in index.links}
    for name in (
        "Clients",
        "Products",
        "Sales",
        "Purchases",
        "Returns",
        "Shipments",
        "Client Complaints",
        "Countries",
        "Regions",
        "Channels",
    ):
        assert ("dataObject", name) in mapped, name
    for name in ("Client Name", "Country Name", "Product Category", "Channel Name"):
        assert ("dimension", name) in mapped, name
    for name in ("Total Sales", "Total Returns", "Total Purchases"):
        assert ("measure", name) in mapped, name
    for name in ("Return Rate", "Gross Margin", "Sales YoY Growth"):
        assert ("metric", name) in mapped, name
    assert ("model", "orionbelt_1_commerce") in mapped


def test_namespaces_and_directions(index: ConceptMappingIndex) -> None:
    prefixes = [row.prefix for row in index.namespaces()]
    assert prefixes[0] == "commerce"
    assert {"schema", "gr", "fibo"} <= set(prefixes)
    # The two 'broader' links say the external concept is the wider notion.
    broader = {link.object.name: link.mapping.concept for link in index.find(relation="broader")}
    assert broader == {
        "Regions": "schema:AdministrativeArea",
        "Sales YoY Growth": "commerce:RevenueGrowth",
    }


def test_graph_carries_the_links_as_skos(graph: Graph) -> None:
    exact = list(graph.subject_objects(SKOS.exactMatch))
    # model + 9 data objects + 4 dimensions + 3 measures + 2 metrics
    assert len(exact) == 19
    assert any(str(o) == COMMERCE + "Revenue" for _, o in exact)
    assert len(list(graph.subject_objects(SKOS.broadMatch))) == 2


def _guide_queries() -> list[str]:
    return re.findall(r"```sparql\n(.*?)```", _GUIDE.read_text(encoding="utf-8"), re.S)


@pytest.mark.parametrize("query", _guide_queries(), ids=lambda q: q.strip().splitlines()[0][:60])
def test_every_guide_query_runs_against_the_demo_graph(graph: Graph, query: str) -> None:
    result = execute_sparql(graph, query)
    assert result.type in ("select", "ask")
    if result.type == "ask":
        assert result.boolean is not None
    elif "skos:" in query:
        # The mapping examples exist to show the demo's links: they must find them.
        assert result.results, "a mapping example should find the demo's links"
        if "https://example.com/ontology/commerce/" in query:
            # The namespace example must follow every SKOS relation, not just exact:
            # the demo's 'broader' link to commerce:RevenueGrowth is in that namespace.
            concepts = {row["concept"] for row in result.results}
            assert COMMERCE + "RevenueGrowth" in concepts
