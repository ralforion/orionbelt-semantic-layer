"""ConceptMappingIndex: the lookups behind the concept-mapping discovery API."""

from __future__ import annotations

import pytest

from orionbelt.models.concept_links import BUILTIN_PREFIXES, ConceptIriError
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.concept_index import (
    MAPPABLE_TYPES,
    ConceptMappingIndex,
    SemanticObjectRef,
)

CORP = "https://ontology.example.com/business/"

_YAML = f"""\
version: 1.0
ontology:
  prefixes:
    corp: "{CORP}"
    corpfin: "{CORP}finance/"
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    externalConceptMappings:
      - concept: corpfin:NetRevenue
        relation: exact
      - concept: corp:Revenue
        relation: broader
      - concept: https://schema.org/MonetaryAmount
        relation: related
      - concept: urn:isbn:0451450523
        relation: related
      - concept: skos:Concept
        relation: related
  Untouched:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: max
"""


@pytest.fixture(scope="module")
def index() -> ConceptMappingIndex:
    raw, sm = TrackedLoader().load_string(_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.errors == []
    return ConceptMappingIndex(model, "m")


def test_namespace_of_prefers_the_longest_declared_prefix(index: ConceptMappingIndex) -> None:
    assert index.namespace_of(CORP + "finance/NetRevenue") == ("corpfin", CORP + "finance/")
    assert index.namespace_of(CORP + "Revenue") == ("corp", CORP)
    assert index.namespace_of(str(BUILTIN_PREFIXES["skos"]) + "Concept") == (
        "skos",
        BUILTIN_PREFIXES["skos"],
    )


def test_namespace_of_falls_back_to_the_last_separator(index: ConceptMappingIndex) -> None:
    assert index.namespace_of("https://schema.org/MonetaryAmount") == (None, "https://schema.org/")
    assert index.namespace_of("https://x.example/ns#Thing") == (None, "https://x.example/ns#")
    assert index.namespace_of("urn:isbn:0451450523") == (None, "urn:isbn:0451450523")


def test_namespaces_are_ordered_most_used_first(index: ConceptMappingIndex) -> None:
    rows = [(r.prefix, r.mapping_count) for r in index.namespaces()]
    assert rows[0][1] == 1  # every namespace used once here: alphabetical by namespace
    assert [r.namespace for r in index.namespaces()] == sorted(
        r.namespace for r in index.namespaces()
    )


def test_find_combines_filters(index: ConceptMappingIndex) -> None:
    assert len(index.find()) == 5
    assert len(index.find(relation="related")) == 3
    assert len(index.find(namespace="corp")) == 2  # corp covers corpfin too
    assert len(index.find(namespace="corpfin")) == 1
    assert len(index.find(namespace="https://schema.org/")) == 1
    assert [link.mapping.concept for link in index.find(concept="corpfin:NetRevenue")] == [
        "corpfin:NetRevenue"
    ]
    assert index.find(concept=CORP + "finance/NetRevenue") == index.find(
        concept="corpfin:NetRevenue"
    )
    assert index.find(types=["dimension"]) == []


def test_find_rejects_an_unexpandable_concept(index: ConceptMappingIndex) -> None:
    with pytest.raises(ConceptIriError):
        index.find(concept="nope:Thing")


def test_find_rejects_a_namespace_that_is_neither_prefix_nor_iri(
    index: ConceptMappingIndex,
) -> None:
    """A typo must not read as "nothing in that namespace"."""
    with pytest.raises(ConceptIriError) as exc:
        index.find(namespace="acme")
    assert exc.value.code == "UNKNOWN_ONTOLOGY_PREFIX"
    assert "corp" in exc.value.suggestions
    assert index.resolve_namespace("corp") == CORP
    assert index.resolve_namespace("https://schema.org/") == "https://schema.org/"


def test_unmapped_skips_synthesized_counts(index: ConceptMappingIndex) -> None:
    assert index.unmapped() == [
        SemanticObjectRef("model", "m"),
        SemanticObjectRef("dataObject", "Orders"),
        SemanticObjectRef("measure", "Untouched"),
    ]
    assert index.unmapped(["measure"]) == [SemanticObjectRef("measure", "Untouched")]
    assert set(MAPPABLE_TYPES) == {"model", "dataObject", "dimension", "measure", "metric", "rule"}
