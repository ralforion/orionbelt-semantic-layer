"""Bidirectional ontology-drift guard (v2.7.6, issue #84).

v2.7.5 shipped a one-direction guard (``test_ontology_drift.py``) that
asserts every OBML modeling field has a matching ``obsl:*`` property
in ``ontology/obsl.ttl``. But the other direction — *does the exporter
actually emit every property the ontology declares?* — wasn't covered.
Result: ``CustomExtension``, ``ModelExample``, ``WithinGroup``,
``delimiter``, ``hasWithinGroup``, ``hasCustomExtension``,
``hasExample``, ``numClass`` all shipped in the ontology but the
exporter silently dropped them on export. Authors who put
``customExtensions:`` or ``examples:`` in their model saw them vanish
from the RDF graph.

This file feeds a model that exercises every newly-added OBML field
into ``export_obsl`` and asserts the corresponding ``obsl:*`` triples
appear in the resulting graph.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.obsl.exporter import export_obsl
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_ROOT = Path(__file__).resolve().parents[2]

_YAML = """
version: 1.0
name: drift_guard_model
description: Exercises every OBML field that v2.7.5 added to the ontology
dataObjects:
  Orders:
    code: ORDERS
    description: Fact table for orders
    columns:
      Order ID:
        code: OID
        abstractType: string
        primaryKey: true
      Customer ID:
        code: CID
        abstractType: string
      Amount:
        code: AMT
        abstractType: float
        numClass: additive
    customExtensions:
      - vendor: osi
        data: '{"ai_context": "order facts"}'
dimensions:
  Order ID:
    dataObject: Orders
    column: Order ID
    resultType: string
measures:
  Customer List:
    columns:
      - dataObject: Orders
        column: Customer ID
    aggregation: listagg
    resultType: string
    delimiter: ', '
    withinGroup:
      column:
        dataObject: Orders
        column: Customer ID
      order: ASC
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    resultType: float
examples:
  - name: revenue_by_customer
    description: Total revenue grouped by customer
    intentTags: [revenue, customer]
    query:
      select:
        measures: [Total Revenue]
customExtensions:
  - vendor: osi
    data: '{"ai_context": "v2.7.6 sample"}'
  - vendor: governance
    data: 'classification: internal'
ontology:
  prefixes:
    corp: "https://ontology.example.com/business/"
externalConceptMappings:
  - concept: corp:SalesModel
    relation: exact
    justification: curated
    source: drift-guard
"""


@pytest.fixture(scope="module")
def graph():
    raw, sm = TrackedLoader().load_string(_YAML)
    model, vr = ReferenceResolver().resolve(raw, sm)
    assert vr.valid, vr.errors
    return export_obsl(model, "test_model_id")


def _has_predicate(g, predicate_local_name: str) -> bool:
    """Return True iff any triple uses ``obsl:<predicate_local_name>``."""
    from rdflib import URIRef

    obsl_predicate = URIRef(f"https://ralforion.com/ns/obsl#{predicate_local_name}")
    return any(True for _ in g.triples((None, obsl_predicate, None)))


def _has_class_instance(g, class_local_name: str) -> bool:
    """Return True iff any subject is typed as ``obsl:<class_local_name>``."""
    from rdflib import RDF, URIRef

    cls = URIRef(f"https://ralforion.com/ns/obsl#{class_local_name}")
    return any(True for _ in g.triples((None, RDF.type, cls)))


# --- Properties added in v2.7.5 (#82) that the exporter must emit -----------


def test_emits_numclass(graph) -> None:
    assert _has_predicate(graph, "numClass"), "Column.num_class not emitted as obsl:numClass"


def test_emits_primarykey(graph) -> None:
    assert _has_predicate(graph, "primaryKey"), "Column.primary_key not emitted as obsl:primaryKey"


def test_emits_delimiter(graph) -> None:
    assert _has_predicate(graph, "delimiter"), "Measure.delimiter not emitted as obsl:delimiter"


def test_emits_within_group(graph) -> None:
    assert _has_predicate(graph, "hasWithinGroup"), "Measure.within_group missing the link"
    assert _has_class_instance(graph, "WithinGroup"), "No obsl:WithinGroup instance created"
    assert _has_predicate(graph, "withinGroupOrder"), "WithinGroup.order not emitted"


def test_emits_custom_extensions(graph) -> None:
    assert _has_predicate(graph, "hasCustomExtension"), "customExtensions not emitted"
    assert _has_class_instance(graph, "CustomExtension"), "No obsl:CustomExtension instance"
    assert _has_predicate(graph, "vendor"), "CustomExtension.vendor not emitted"
    assert _has_predicate(graph, "extensionData"), "CustomExtension.data not emitted"


def test_emits_examples(graph) -> None:
    assert _has_predicate(graph, "hasExample"), "examples not emitted"
    assert _has_class_instance(graph, "ModelExample"), "No obsl:ModelExample instance created"
    assert _has_predicate(graph, "exampleName"), "ModelExample.name not emitted"
    assert _has_predicate(graph, "exampleDescription"), "ModelExample.description not emitted"
    assert _has_predicate(graph, "exampleQuery"), "ModelExample.query not emitted"
    assert _has_predicate(graph, "intentTag"), "ModelExample.intent_tags not emitted"


def test_custom_extensions_attached_to_multiple_subject_types(graph) -> None:
    """customExtensions can be on any modeling element — the test YAML
    has them on the model itself (2 vendors) and on the Orders data
    object (1 vendor). So we expect at least 3 hasCustomExtension
    triples coming from at least 2 distinct subjects.
    """
    from rdflib import URIRef

    has_ext = URIRef("https://ralforion.com/ns/obsl#hasCustomExtension")
    triples = list(graph.triples((None, has_ext, None)))
    assert len(triples) >= 3, f"Expected >= 3 hasCustomExtension triples, got {len(triples)}"
    distinct_subjects = {s for s, _, _ in triples}
    assert len(distinct_subjects) >= 2, (
        "customExtensions only attached to one subject — the test model "
        "puts them on both the SemanticModel and the Orders dataObject."
    )


# --- External concept mappings (context/ontology plan, PR 2) -----------------


def test_emits_external_concept_mapping(graph) -> None:
    assert _has_class_instance(graph, "ExternalConceptMapping")
    for prop in (
        "hasExternalConceptMapping",
        "sourceObject",
        "targetConcept",
        "authoredConcept",
        "mappingRelation",
        "mappingJustification",
        "mappingSource",
    ):
        assert _has_predicate(graph, prop), prop
