"""Tests for OBSL-Core 0.1 exporter, SPARQL execution, and graph storage."""

from __future__ import annotations

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS

from orionbelt.models.semantic import SemanticModel
from orionbelt.obsl.exporter import BASE, OBSL, export_obsl
from orionbelt.obsl.sparql import SPARQLUpdateError, execute_sparql
from orionbelt.service.model_store import ModelStore
from tests.conftest import SAMPLE_MODEL_YAML

# ---------------------------------------------------------------------------
# Exporter — triples for each entity type
# ---------------------------------------------------------------------------


class TestExporterModel:
    def test_model_container(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1")
        assert (uri, RDF.type, OBSL.SemanticModel) in g

    def test_model_has_data_objects(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1")
        objects = list(g.objects(uri, OBSL.hasDataObject))
        assert len(objects) == 3  # Customers, Products, Orders


class TestExporterDataObjects:
    def test_data_object_type(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/data-object/customers")
        assert (uri, RDF.type, OBSL.DataObject) in g

    def test_data_object_properties(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/data-object/customers")
        assert (uri, RDFS.label, Literal("Customers")) in g
        assert (uri, OBSL.code, Literal("CUSTOMERS")) in g
        assert (uri, OBSL.database, Literal("WAREHOUSE")) in g
        assert (uri, OBSL["schema"], Literal("PUBLIC")) in g

    def test_synonyms(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/data-object/customers")
        synonyms = {str(o) for o in g.objects(uri, OBSL.synonym)}
        assert "client" in synonyms
        assert "buyer" in synonyms


class TestExporterColumns:
    def test_column_type(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/data-object/customers/column/country")
        assert (uri, RDF.type, OBSL.Column) in g

    def test_column_properties(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/data-object/customers/column/country")
        assert (uri, RDFS.label, Literal("Country")) in g
        assert (uri, OBSL.code, Literal("COUNTRY")) in g
        assert (uri, OBSL.resultType, Literal("string")) in g

    def test_has_column_link(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        obj = URIRef(f"{BASE}t1/data-object/customers")
        col = URIRef(f"{BASE}t1/data-object/customers/column/country")
        assert (obj, OBSL.hasColumn, col) in g


class TestExporterJoins:
    def test_join_type(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/join/orders-to-customers")
        assert (uri, RDF.type, OBSL.Join) in g

    def test_join_target(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        join = URIRef(f"{BASE}t1/join/orders-to-customers")
        target = URIRef(f"{BASE}t1/data-object/customers")
        assert (join, OBSL.joinTo, target) in g

    def test_join_cardinality(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        join = URIRef(f"{BASE}t1/join/orders-to-customers")
        assert (join, OBSL.cardinality, Literal("many-to-one")) in g

    def test_join_columns(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        join = URIRef(f"{BASE}t1/join/orders-to-customers")
        col_from = URIRef(f"{BASE}t1/data-object/orders/column/order-customer-id")
        col_to = URIRef(f"{BASE}t1/data-object/customers/column/customer-id")
        assert (join, OBSL.columnFrom, col_from) in g
        assert (join, OBSL.columnTo, col_to) in g


class TestExporterDimensions:
    def test_dimension_type(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/dimension/customer-country")
        assert (uri, RDF.type, OBSL.Dimension) in g

    def test_dimension_properties(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/dimension/customer-country")
        assert (uri, RDFS.label, Literal("Customer Country")) in g
        assert (uri, OBSL.resultType, Literal("string")) in g

    def test_dimension_links(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        dim = URIRef(f"{BASE}t1/dimension/customer-country")
        obj = URIRef(f"{BASE}t1/data-object/customers")
        col = URIRef(f"{BASE}t1/data-object/customers/column/country")
        assert (dim, OBSL.dataObject, obj) in g
        assert (dim, OBSL.column, col) in g

    def test_time_grain(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        dim = URIRef(f"{BASE}t1/dimension/order-date")
        assert (dim, OBSL.timeGrain, Literal("month")) in g


class TestExporterMeasures:
    def test_measure_type(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/revenue")
        assert (uri, RDF.type, OBSL.Measure) in g

    def test_measure_properties(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/revenue")
        assert (uri, RDFS.label, Literal("Revenue")) in g
        assert (uri, OBSL.aggregation, Literal("sum")) in g
        assert (uri, OBSL.resultType, Literal("float")) in g

    def test_expression_source(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/revenue")
        expr = Literal("{[Orders].[Price]} * {[Orders].[Quantity]}")
        assert (uri, OBSL.expressionSource, expr) in g

    def test_source_column(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/order-count")
        col = URIRef(f"{BASE}t1/data-object/orders/column/order-id")
        assert (uri, OBSL.sourceColumn, col) in g

    def test_total_flag(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/grand-total-revenue")
        assert (uri, OBSL.total, Literal(True)) in g

    def test_measure_synonyms(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/revenue")
        synonyms = {str(o) for o in g.objects(uri, OBSL.synonym)}
        assert "sales" in synonyms
        assert "income" in synonyms


class TestExporterMetrics:
    def test_metric_type(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/revenue-per-order")
        assert (uri, RDF.type, OBSL.Metric) in g

    def test_derived_metric(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/revenue-per-order")
        assert (uri, OBSL.metricType, Literal("derived")) in g
        expr = Literal("{[Revenue]} / {[Order Count]}")
        assert (uri, OBSL.expressionSource, expr) in g

    def test_references_measure(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        met = URIRef(f"{BASE}t1/metric/revenue-per-order")
        rev = URIRef(f"{BASE}t1/measure/revenue")
        cnt = URIRef(f"{BASE}t1/measure/order-count")
        assert (met, OBSL.referencesMeasure, rev) in g
        assert (met, OBSL.referencesMeasure, cnt) in g

    def test_cumulative_metric(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/running-revenue")
        assert (uri, OBSL.metricType, Literal("cumulative")) in g
        rev = URIRef(f"{BASE}t1/measure/revenue")
        assert (uri, OBSL.baseMeasure, rev) in g

    def test_metric_description(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/running-revenue")
        assert (uri, RDFS.comment, Literal("Running total of revenue over time")) in g


class TestExporterSerialization:
    def test_turtle_output(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        turtle = g.serialize(format="turtle")
        assert "obsl:SemanticModel" in turtle
        assert "rdfs:label" in turtle
        assert "obsl:hasDataObject" in turtle


# ---------------------------------------------------------------------------
# SPARQL execution
# ---------------------------------------------------------------------------


class TestSPARQL:
    def test_select_measures(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        result = execute_sparql(
            g,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT ?label WHERE {
                ?m a obsl:Measure ; rdfs:label ?label .
            }
            """,
        )
        assert result.type == "select"
        labels = {r["label"] for r in result.results}
        assert "Revenue" in labels
        assert "Order Count" in labels

    def test_ask_true(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        result = execute_sparql(
            g,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            ASK { ?x a obsl:Dimension }
            """,
        )
        assert result.type == "ask"
        assert result.boolean is True

    def test_ask_false(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        result = execute_sparql(
            g,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            ASK { ?x a <http://example.org/nonexistent> }
            """,
        )
        assert result.type == "ask"
        assert result.boolean is False

    def test_reject_insert(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        with pytest.raises(SPARQLUpdateError):
            execute_sparql(g, "INSERT DATA { <x> <y> <z> }")

    def test_reject_delete(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        with pytest.raises(SPARQLUpdateError):
            execute_sparql(g, "DELETE DATA { <x> <y> <z> }")

    def test_reject_drop(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        with pytest.raises(SPARQLUpdateError):
            execute_sparql(g, "DROP GRAPH <urn:g>")

    def test_metrics_referencing_measure(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        result = execute_sparql(
            g,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT ?label WHERE {
                ?metric a obsl:Metric ;
                        obsl:referencesMeasure ?measure ;
                        rdfs:label ?label .
                ?measure rdfs:label "Revenue" .
            }
            """,
        )
        labels = {r["label"] for r in result.results}
        assert "Revenue per Order" in labels
        assert "Revenue Share" in labels

    def test_joins_from_data_object(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        result = execute_sparql(
            g,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT ?target ?cardinality WHERE {
                ?obj rdfs:label "Orders" ;
                     obsl:hasJoin ?join .
                ?join obsl:joinTo ?target ;
                      obsl:cardinality ?cardinality .
            }
            """,
        )
        assert result.type == "select"
        assert len(result.results) == 2  # Orders → Customers, Orders → Products

    def test_select_variables(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        result = execute_sparql(
            g,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT ?name ?agg WHERE {
                ?m a obsl:Measure ; rdfs:label ?name ; obsl:aggregation ?agg .
            }
            """,
        )
        assert "name" in result.variables
        assert "agg" in result.variables


# ---------------------------------------------------------------------------
# ModelStore graph integration
# ---------------------------------------------------------------------------


class TestModelStoreGraph:
    def test_graph_created_on_load(self) -> None:
        store = ModelStore()
        result = store.load_model(SAMPLE_MODEL_YAML)
        artifact = store.get_graph(result.model_id)
        assert artifact.turtle
        assert artifact.generated_at > 0
        # Verify triples exist
        m_uri = URIRef(f"{BASE}{result.model_id}")
        assert (m_uri, RDF.type, OBSL.SemanticModel) in artifact.graph

    def test_graph_removed_on_unload(self) -> None:
        store = ModelStore()
        result = store.load_model(SAMPLE_MODEL_YAML)
        store.remove_model(result.model_id)
        with pytest.raises(KeyError):
            store.get_graph(result.model_id)

    def test_query_graph(self) -> None:
        store = ModelStore()
        result = store.load_model(SAMPLE_MODEL_YAML)
        sparql_result = store.query_graph(
            result.model_id,
            """
            PREFIX obsl: <https://ralforion.com/ns/obsl#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT ?label WHERE {
                ?m a obsl:Measure ; rdfs:label ?label .
            }
            """,
        )
        labels = {r["label"] for r in sparql_result.results}
        assert "Total Revenue" in labels
        assert "Order Count" in labels

    def test_query_graph_reject_update(self) -> None:
        store = ModelStore()
        result = store.load_model(SAMPLE_MODEL_YAML)
        with pytest.raises(SPARQLUpdateError):
            store.query_graph(result.model_id, "INSERT DATA { <a> <b> <c> }")

    def test_get_graph_not_found(self) -> None:
        store = ModelStore()
        with pytest.raises(KeyError):
            store.get_graph("nonexistent")
