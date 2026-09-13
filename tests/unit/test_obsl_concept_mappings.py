"""RDF projection of external concept mappings (PR 2 of the context/ontology plan).

Every mapping becomes a direct ``skos:*Match`` triple from the modeling
element to the expanded target IRI. A mapping with provenance also gets an
``obsl:ExternalConceptMapping`` resource at a deterministic IRI. The
model's prefixes are bound on the graph, and external ontologies are only
ever referenced, never imported.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from orionbelt.models.semantic import ExternalConceptMapping, Measure, SemanticModel
from orionbelt.obsl.exporter import BASE, OBSL, export_obsl
from orionbelt.obsl.sparql import execute_sparql
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

CORP = "https://ontology.example.com/business/"

_YAML = f"""\
version: 1.0
ontology:
  prefixes:
    corp: "{CORP}"
externalConceptMappings:
  - concept: corp:SalesModel
    relation: exact
dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    externalConceptMappings:
      - concept: corp:Order
        relation: exact
    columns:
      ID:
        code: ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
      Order Date:
        code: ORDER_DATE
        abstractType: date
dimensions:
  Order ID:
    dataObject: Orders
    column: ID
    externalConceptMappings:
      - concept: corp:OrderIdentifier
        relation: exact
  Order Month:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month
measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    externalConceptMappings:
      - concept: corp:NetRevenue
        relation: exact
        justification: curated
        source: enterprise-finance-ontology
        ontologyVersion: "2026.1"
        confidence: 0.9
        comment: Approved by Finance
      - concept: https://schema.org/MonetaryAmount
        relation: broader
      - concept: corp:GrossRevenue
        relation: related
      - concept: corp:Sales
        relation: close
      - concept: corp:RevenueQ1
        relation: narrower
metrics:
  Revenue Doubled:
    expression: "{{[Revenue]}} * 2"
    externalConceptMappings:
      - concept: corp:DoubleRevenue
        relation: exact
        justification: lexical
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
    externalConceptMappings:
      - concept: corp:RunningRevenue
        relation: related
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, sm = TrackedLoader().load_string(_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.errors == []
    return model


@pytest.fixture(scope="module")
def graph(model: SemanticModel) -> Graph:
    return export_obsl(model, "t1")


MODEL = URIRef(f"{BASE}t1")
ORDERS = URIRef(f"{BASE}t1/data-object/orders")
ORDER_ID = URIRef(f"{BASE}t1/dimension/order-id")
REVENUE = URIRef(f"{BASE}t1/measure/revenue")
DOUBLED = URIRef(f"{BASE}t1/metric/revenue-doubled")
RUNNING = URIRef(f"{BASE}t1/metric/running-revenue")


class TestDirectSkosTriples:
    @pytest.mark.parametrize(
        "predicate, target",
        [
            (SKOS.exactMatch, CORP + "NetRevenue"),
            (SKOS.broadMatch, "https://schema.org/MonetaryAmount"),
            (SKOS.relatedMatch, CORP + "GrossRevenue"),
            (SKOS.closeMatch, CORP + "Sales"),
            (SKOS.narrowMatch, CORP + "RevenueQ1"),
        ],
    )
    def test_every_relation_maps_to_its_skos_predicate(
        self, graph: Graph, predicate: URIRef, target: str
    ) -> None:
        assert (REVENUE, predicate, URIRef(target)) in graph

    def test_compact_iri_is_expanded_in_the_graph(self, graph: Graph) -> None:
        assert (REVENUE, SKOS.exactMatch, URIRef(CORP + "NetRevenue")) in graph
        assert not any(
            "corp:" in str(o) for _, _, o in graph.triples((REVENUE, SKOS.exactMatch, None))
        )

    @pytest.mark.parametrize(
        "subject, target",
        [
            (MODEL, CORP + "SalesModel"),
            (ORDERS, CORP + "Order"),
            (ORDER_ID, CORP + "OrderIdentifier"),
            (REVENUE, CORP + "NetRevenue"),
            (DOUBLED, CORP + "DoubleRevenue"),
        ],
        ids=["model", "data-object", "dimension", "measure", "metric"],
    )
    def test_every_supported_object_type_is_a_subject(
        self, graph: Graph, subject: URIRef, target: str
    ) -> None:
        assert (subject, SKOS.exactMatch, URIRef(target)) in graph

    def test_cumulative_metric_too(self, graph: Graph) -> None:
        assert (RUNNING, SKOS.relatedMatch, URIRef(CORP + "RunningRevenue")) in graph

    def test_external_ontology_is_not_imported(self, graph: Graph) -> None:
        """The target IRI is referenced, never described: no triples about it."""
        target = URIRef(CORP + "NetRevenue")
        assert list(graph.triples((target, None, None))) == []
        assert (MODEL, OWL.imports, URIRef(CORP)) not in graph


class TestProvenanceResource:
    MAP = URIRef(f"{REVENUE}/concept-mapping/https-ontology-example-com-business-netrevenue")

    def test_resource_carries_every_field(self, graph: Graph) -> None:
        g = graph
        assert (REVENUE, OBSL.hasExternalConceptMapping, self.MAP) in g
        assert (self.MAP, RDF.type, OBSL.ExternalConceptMapping) in g
        assert (self.MAP, OBSL.sourceObject, REVENUE) in g
        assert (self.MAP, OBSL.targetConcept, URIRef(CORP + "NetRevenue")) in g
        assert (self.MAP, OBSL.authoredConcept, Literal("corp:NetRevenue")) in g
        assert (self.MAP, OBSL.mappingRelation, Literal("exact")) in g
        assert (self.MAP, OBSL.mappingJustification, Literal("curated")) in g
        assert (self.MAP, OBSL.mappingSource, Literal("enterprise-finance-ontology")) in g
        assert (self.MAP, OBSL.ontologyVersion, Literal("2026.1")) in g
        assert (self.MAP, OBSL.mappingComment, Literal("Approved by Finance")) in g

    def test_confidence_is_an_xsd_decimal(self, graph: Graph) -> None:
        [conf] = list(graph.objects(self.MAP, OBSL.confidence))
        assert isinstance(conf, Literal)
        assert conf.datatype == XSD.decimal
        assert conf.toPython() == Decimal("0.9")

    def test_bare_mapping_gets_no_resource(self, graph: Graph) -> None:
        """Concept + relation only: the direct triple says everything."""
        for subject in (MODEL, ORDERS, ORDER_ID, RUNNING):
            assert list(graph.objects(subject, OBSL.hasExternalConceptMapping)) == []
        # Revenue has five mappings, one with provenance.
        assert len(list(graph.objects(REVENUE, OBSL.hasExternalConceptMapping))) == 1

    def test_justification_alone_earns_a_resource(self, graph: Graph) -> None:
        [map_uri] = list(graph.objects(DOUBLED, OBSL.hasExternalConceptMapping))
        assert (map_uri, OBSL.mappingJustification, Literal("lexical")) in graph
        assert list(graph.objects(map_uri, OBSL.mappingSource)) == []

    def test_resource_identity_is_deterministic(self, model: SemanticModel) -> None:
        a = export_obsl(model, "t1")
        b = export_obsl(model, "t1")
        assert set(a.subjects(RDF.type, OBSL.ExternalConceptMapping)) == set(
            b.subjects(RDF.type, OBSL.ExternalConceptMapping)
        )
        assert a.isomorphic(b)

    def test_vocabulary_is_embedded(self, graph: Graph) -> None:
        """The graph is self-contained: the mapping vocabulary is declared in it."""
        assert (OBSL.ExternalConceptMapping, RDF.type, OWL.Class) in graph
        assert (OBSL.hasExternalConceptMapping, RDF.type, OWL.ObjectProperty) in graph
        assert (OBSL.sourceObject, OWL.inverseOf, OBSL.hasExternalConceptMapping) in graph
        assert (OBSL.targetConcept, RDF.type, OWL.FunctionalProperty) in graph
        assert list(graph.objects(OBSL.targetConcept, RDFS.range)) == []
        assert (OBSL.confidence, RDF.type, OWL.DatatypeProperty) in graph


class TestPrefixBindings:
    def test_model_prefixes_and_skos_are_bound(self, graph: Graph) -> None:
        bound = dict(graph.namespaces())
        assert str(bound["corp"]) == CORP
        assert str(bound["skos"]) == str(SKOS)
        turtle = graph.serialize(format="turtle")
        assert "corp:NetRevenue" in turtle
        assert "skos:exactMatch" in turtle

    def test_model_prefix_cannot_displace_a_core_binding(self) -> None:
        raw, _ = TrackedLoader().load_string(
            'version: 1.0\nontology:\n  prefixes:\n    obsl: "https://elsewhere.example/"\n'
            "dataObjects: {}\ndimensions: {}\nmeasures: {}\n"
        )
        model, result = ReferenceResolver().resolve(raw)
        assert result.errors == []
        g = export_obsl(model, "t1")
        assert str(dict(g.namespaces())["obsl"]) == str(OBSL)


class TestQueries:
    def test_find_objects_by_external_iri(self, graph: Graph) -> None:
        result = execute_sparql(
            graph,
            "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "SELECT ?label WHERE { ?s skos:exactMatch <"
            + CORP
            + "NetRevenue> ; rdfs:label ?label . }",
        )
        assert [row["label"] for row in result.results] == ["Revenue"]

    def test_list_all_mappings(self, graph: Graph) -> None:
        result = execute_sparql(
            graph,
            "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(?p IN (skos:exactMatch, "
            "skos:closeMatch, skos:broadMatch, skos:narrowMatch, skos:relatedMatch)) }",
        )
        assert len(result.results) == 10


class TestPythonBuiltModel:
    def test_unexpanded_mapping_is_expanded_at_export(self) -> None:
        """A model built without the resolver has no expanded_iri; export expands it."""
        model = SemanticModel(
            measures={
                "M": Measure(
                    name="M",
                    aggregation="count",
                    external_concept_mappings=[
                        ExternalConceptMapping(concept="skos:Concept", relation="related")
                    ],
                )
            }
        )
        g = export_obsl(model, "t1")
        assert (
            URIRef(f"{BASE}t1/measure/m"),
            SKOS.relatedMatch,
            URIRef(str(SKOS) + "Concept"),
        ) in g


class TestNoMappings:
    def test_plain_model_has_no_skos_triples(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        for pred in (
            SKOS.exactMatch,
            SKOS.closeMatch,
            SKOS.broadMatch,
            SKOS.narrowMatch,
            SKOS.relatedMatch,
            OBSL.hasExternalConceptMapping,
        ):
            assert list(g.triples((None, pred, None))) == []
