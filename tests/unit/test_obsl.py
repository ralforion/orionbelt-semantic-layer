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

    def test_primary_key_triple_emitted(self, sales_model: SemanticModel) -> None:
        # Sales fixture marks Customer ID as primary key.
        g = export_obsl(sales_model, "t1")
        pk_col = URIRef(f"{BASE}t1/data-object/customers/column/customer-id")
        assert (pk_col, OBSL.primaryKey, Literal(True)) in g

    def test_primary_key_omitted_for_non_pk(self, sales_model: SemanticModel) -> None:
        # Customer Name is not a primary key — no triple should be emitted.
        g = export_obsl(sales_model, "t1")
        non_pk = URIRef(f"{BASE}t1/data-object/customers/column/customer-name")
        assert (non_pk, OBSL.primaryKey, Literal(True)) not in g


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


class TestExporterJoinOrderIndependence:
    """Regression: join target columns must resolve regardless of data object order."""

    LATE_TARGET_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Customer FK:
        code: CUSTOMER_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Customer FK
        columnsTo:
          - Customer ID

  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string

dimensions:
  Customer ID:
    dataObject: Customers
    column: Customer ID
    resultType: string

measures:
  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
"""

    def _build_model(self) -> SemanticModel:
        from orionbelt.parser.loader import TrackedLoader
        from orionbelt.parser.resolver import ReferenceResolver

        raw, sm = TrackedLoader().load_string(self.LATE_TARGET_YAML)
        model, result = ReferenceResolver().resolve(raw, sm)
        assert result.valid
        return model

    def test_column_to_present_when_target_declared_later(self) -> None:
        model = self._build_model()
        g = export_obsl(model, "t1")
        join = URIRef(f"{BASE}t1/join/orders-to-customers")
        col_to = URIRef(f"{BASE}t1/data-object/customers/column/customer-id")
        assert (join, OBSL.columnTo, col_to) in g


class TestExporterSecondaryJoinURIs:
    """Regression: multiple joins to the same target must get distinct URIs."""

    SECONDARY_JOIN_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Customer FK:
        code: CUSTOMER_ID
        abstractType: string
      Shipping Customer FK:
        code: SHIP_CUSTOMER_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Customer FK
        columnsTo:
          - Customer ID
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Shipping Customer FK
        columnsTo:
          - Customer ID
        secondary: true
        pathName: ship_to

  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string

dimensions:
  Customer ID:
    dataObject: Customers
    column: Customer ID
    resultType: string

measures:
  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
"""

    def _build_model(self) -> SemanticModel:
        from orionbelt.parser.loader import TrackedLoader
        from orionbelt.parser.resolver import ReferenceResolver

        raw, sm = TrackedLoader().load_string(self.SECONDARY_JOIN_YAML)
        model, result = ReferenceResolver().resolve(raw, sm)
        assert result.valid
        return model

    def test_secondary_join_gets_distinct_uri(self) -> None:
        model = self._build_model()
        g = export_obsl(model, "t1")
        primary = URIRef(f"{BASE}t1/join/orders-to-customers")
        secondary = URIRef(f"{BASE}t1/join/orders-to-customers/ship-to")
        assert (primary, RDF.type, OBSL.Join) in g
        assert (secondary, RDF.type, OBSL.Join) in g

    def test_secondary_join_columns_not_mixed(self) -> None:
        model = self._build_model()
        g = export_obsl(model, "t1")
        primary = URIRef(f"{BASE}t1/join/orders-to-customers")
        secondary = URIRef(f"{BASE}t1/join/orders-to-customers/ship-to")
        primary_from = set(g.objects(primary, OBSL.columnFrom))
        secondary_from = set(g.objects(secondary, OBSL.columnFrom))
        assert len(primary_from) == 1
        assert len(secondary_from) == 1
        assert primary_from != secondary_from


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

    def test_synthesized_count_measures_exported(self, sales_model: SemanticModel) -> None:
        """Auto-synthesized row-count measures are emitted as obsl:Measure.

        They are not in ``model.measures`` (declared) but come from
        ``effective_measures``, so the graph must build from the latter.
        """
        g = export_obsl(sales_model, "t1")
        synthesized = [n for n in sales_model.effective_measures if n not in sales_model.measures]
        assert synthesized, "expected the sales fixture to synthesize count measures"
        uri = URIRef(f"{BASE}t1/measure/orders-count")
        assert (uri, RDF.type, OBSL.Measure) in g
        assert (uri, RDFS.label, Literal("Orders Count")) in g
        assert (uri, OBSL.aggregation, Literal("count")) in g
        assert (uri, OBSL.resultType, Literal("int")) in g
        # SHACL source form: grain anchor (not sourceColumn / expressionSource).
        assert (uri, OBSL.anchoredTo, URIRef(f"{BASE}t1/data-object/orders")) in g
        assert not list(g.objects(uri, OBSL.sourceColumn))
        assert not list(g.objects(uri, OBSL.expressionSource))

    def test_expression_measure_references_columns(self, sales_model: SemanticModel) -> None:
        """An expression measure keeps obsl:expressionSource as its source form;
        its column dependencies use obsl:referencesColumn (not obsl:sourceColumn),
        so it stays valid against the mutually-exclusive SHACL MeasureShape."""
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/average-order-value")
        price = URIRef(f"{BASE}t1/data-object/orders/column/price")
        qty = URIRef(f"{BASE}t1/data-object/orders/column/quantity")
        assert (uri, OBSL.referencesColumn, price) in g
        assert (uri, OBSL.referencesColumn, qty) in g
        # expressionSource is the source form; sourceColumn stays reserved for
        # declared columns[] (this measure declares none).
        assert list(g.objects(uri, OBSL.expressionSource))
        assert not list(g.objects(uri, OBSL.sourceColumn))

    def test_filter_expression(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/us-revenue")
        expr_values = list(g.objects(uri, OBSL.filterExpression))
        assert len(expr_values) == 1
        expr = str(expr_values[0])
        assert "Customers.Country" in expr
        assert "'US'" in expr

    def test_no_filter_expression_when_empty(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/measure/revenue")
        assert not list(g.objects(uri, OBSL.filterExpression))

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

    # -- Cumulative metric extended properties --------------------------------

    def test_cumulative_metric_subtype(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/running-revenue")
        assert (uri, RDF.type, OBSL.CumulativeMetric) in g

    def test_cumulative_time_dimension(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/running-revenue")
        dim = URIRef(f"{BASE}t1/dimension/order-date")
        assert (uri, OBSL.timeDimension, dim) in g

    def test_cumulative_type_property(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/running-revenue")
        assert (uri, OBSL.cumulativeType, Literal("sum")) in g

    def test_cumulative_window(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/rolling-3m-revenue")
        assert (uri, OBSL.window, Literal(3)) in g

    # -- Period-over-period metric extended properties ------------------------

    def test_pop_metric_subtype(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/revenue-mom-change")
        assert (uri, RDF.type, OBSL.PeriodOverPeriodMetric) in g

    def test_pop_time_dimension(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/revenue-mom-change")
        dim = URIRef(f"{BASE}t1/dimension/order-date")
        assert (uri, OBSL.timeDimension, dim) in g

    def test_pop_comparison_properties(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        uri = URIRef(f"{BASE}t1/metric/revenue-mom-change")
        assert (uri, OBSL.timeGrain, Literal("month")) in g
        assert (uri, OBSL.offset, Literal(-1)) in g
        assert (uri, OBSL.offsetGrain, Literal("month")) in g
        assert (uri, OBSL.comparison, Literal("difference")) in g


class TestExporterAxioms:
    """OWL axioms embedded in exported graphs."""

    def test_all_disjoint_classes(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        from rdflib.namespace import OWL as OWL_NS

        disjoint_nodes = list(g.subjects(RDF.type, OWL_NS.AllDisjointClasses))
        assert len(disjoint_nodes) == 1
        # Collect the members list
        members_head = g.value(disjoint_nodes[0], OWL_NS.members)
        assert members_head is not None
        members = list(g.items(members_head))
        expected = {
            OBSL.SemanticModel,
            OBSL.DataObject,
            OBSL.Column,
            OBSL.Join,
            OBSL.Dimension,
            OBSL.Measure,
            OBSL.Metric,
        }
        assert set(members) == expected

    def test_metric_subclass_disjointness(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        from rdflib.namespace import OWL as OWL_NS

        assert (OBSL.CumulativeMetric, OWL_NS.disjointWith, OBSL.PeriodOverPeriodMetric) in g

    def test_functional_properties(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        from rdflib.namespace import OWL as OWL_NS

        functional_props = {
            OBSL.joinTo,
            OBSL.code,
            OBSL.database,
            OBSL["schema"],
            OBSL.resultType,
            OBSL.aggregation,
            OBSL.metricType,
            OBSL.cardinality,
            OBSL.expressionSource,
            OBSL.filterExpression,
            OBSL.dataObject,
            OBSL.column,
            OBSL.cumulativeType,
            OBSL.window,
            OBSL.grainToDate,
            OBSL.offset,
            OBSL.offsetGrain,
            OBSL.comparison,
        }
        for prop in functional_props:
            assert (prop, RDF.type, OWL_NS.FunctionalProperty) in g, (
                f"{prop} should be declared owl:FunctionalProperty"
            )

    def test_inverse_property(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        from rdflib.namespace import OWL as OWL_NS

        assert (OBSL.belongsToModel, OWL_NS.inverseOf, OBSL.hasDataObject) in g


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

    def test_reject_construct(self, sales_model: SemanticModel) -> None:
        g = export_obsl(sales_model, "t1")
        with pytest.raises(ValueError, match="CONSTRUCT is not allowed"):
            execute_sparql(
                g,
                """
                PREFIX obsl: <https://ralforion.com/ns/obsl#>
                CONSTRUCT { ?s a obsl:Measure } WHERE { ?s a obsl:Measure }
                """,
            )

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
