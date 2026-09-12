"""External concept mappings: the OBML mapping foundation.

``externalConceptMappings`` link a model, data object, dimension, measure or
metric to a concept in an external ontology. These tests pin the contract of
the first PR of ``PLAN_OBSL_CONTEXT_ONTOLOGY_SYNC``: prefixes and IRIs
normalize to absolute IRIs, every invalid shape is a structured error with a
source span, existing models load unchanged, and compiled SQL and cache keys
are byte-identical with and without mappings.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from orionbelt.cache.key import build_cache_key
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.concept_links import (
    BUILTIN_PREFIXES,
    ConceptIriError,
    effective_prefixes,
    expand_concept,
    is_absolute_iri,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import (
    ExternalConceptMapping,
    ExternalConceptRelation,
    MappingJustification,
    SemanticModel,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.merger import ExtendsMerger, MergeError
from orionbelt.parser.resolver import ReferenceResolver

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "obml-schema.json").read_text()
)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)

CORP = "https://ontology.example.com/business/"

_BASE = """\
version: 1.0
ontology:
  prefixes:
    corp: "https://ontology.example.com/business/"
    fibo: "https://spec.edmcouncil.org/fibo/ontology/"
dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    columns:
      ID:
        code: ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
      Order Date:
        code: ORDER_DATE
        abstractType: date
dimensions:
  Order ID:
    dataObject: Orders
    column: ID
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
metrics:
  Revenue Doubled:
    expression: "{[Revenue]} * 2"
"""


def _resolve(yaml_text: str) -> tuple[SemanticModel, list[SemanticError]]:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    return model, result.errors


def _codes(errors: list[SemanticError]) -> list[str]:
    return [e.code for e in errors]


def _with_measure_mappings(block: str) -> str:
    """The base model with an indented mapping block under the Revenue measure."""
    indented = "\n".join(f"    {line}" if line else line for line in block.splitlines())
    return _BASE.replace(
        "    aggregation: sum\n",
        f"    aggregation: sum\n{indented}\n",
    )


# ───────────────────────────── valid shapes ─────────────────────────────


class TestValidMappings:
    def test_compact_iri_expands(self) -> None:
        model, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: exact\n"
            )
        )
        assert errors == []
        [mapping] = model.measures["Revenue"].external_concept_mappings
        assert mapping.concept == "corp:NetRevenue"
        assert mapping.expanded_iri == CORP + "NetRevenue"
        assert mapping.relation is ExternalConceptRelation.EXACT
        assert mapping.justification is None

    def test_full_iri_is_kept_verbatim(self) -> None:
        model, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n"
                "  - concept: https://schema.org/MonetaryAmount\n"
                "    relation: close\n"
            )
        )
        assert errors == []
        [mapping] = model.measures["Revenue"].external_concept_mappings
        assert mapping.expanded_iri == "https://schema.org/MonetaryAmount"

    def test_builtin_prefix_needs_no_declaration(self) -> None:
        model, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: skos:Concept\n    relation: related\n"
            )
        )
        assert errors == []
        [mapping] = model.measures["Revenue"].external_concept_mappings
        assert mapping.expanded_iri == BUILTIN_PREFIXES["skos"] + "Concept"

    def test_redeclaring_a_builtin_verbatim_is_allowed(self) -> None:
        yaml_text = _BASE.replace("    fibo:", f'    skos: "{BUILTIN_PREFIXES["skos"]}"\n    fibo:')
        _, errors = _resolve(yaml_text)
        assert errors == []

    def test_all_metadata_round_trips(self) -> None:
        model, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n"
                "  - concept: corp:NetRevenue\n"
                "    relation: broader\n"
                "    justification: curated\n"
                "    source: enterprise-finance-ontology\n"
                '    ontologyVersion: "2026.1"\n'
                "    confidence: 0.9\n"
                "    comment: Approved by Finance Data Governance\n"
            )
        )
        assert errors == []
        [mapping] = model.measures["Revenue"].external_concept_mappings
        assert mapping.relation is ExternalConceptRelation.BROADER
        assert mapping.justification is MappingJustification.CURATED
        assert mapping.source == "enterprise-finance-ontology"
        assert mapping.ontology_version == "2026.1"
        assert mapping.confidence == pytest.approx(0.9)
        assert mapping.comment == "Approved by Finance Data Governance"

    def test_same_iri_on_different_objects_is_fine(self) -> None:
        yaml_text = _with_measure_mappings(
            "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: exact\n"
        ).replace(
            '    expression: "{[Revenue]} * 2"\n',
            '    expression: "{[Revenue]} * 2"\n'
            "    externalConceptMappings:\n"
            "      - concept: corp:NetRevenue\n"
            "        relation: narrower\n",
        )
        _, errors = _resolve(yaml_text)
        assert errors == []

    @pytest.mark.parametrize("relation", [r.value for r in ExternalConceptRelation])
    def test_every_relation_loads(self, relation: str) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                f"externalConceptMappings:\n  - concept: corp:X\n    relation: {relation}\n"
            )
        )
        assert errors == []

    @pytest.mark.parametrize("justification", [j.value for j in MappingJustification])
    def test_every_justification_loads(self, justification: str) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:X\n    relation: exact\n"
                f"    justification: {justification}\n"
            )
        )
        assert errors == []


class TestSupportedObjects:
    """Mappings load on every object in the initial scope and nowhere else."""

    _ENTRY = "externalConceptMappings:\n  - concept: corp:Thing\n    relation: exact\n"

    def test_model_level(self) -> None:
        model, errors = _resolve(_BASE + self._ENTRY)
        assert errors == []
        assert model.external_concept_mappings[0].expanded_iri == CORP + "Thing"
        assert model.ontology is not None
        assert model.ontology.prefixes["corp"] == CORP

    def test_data_object(self) -> None:
        yaml_text = _BASE.replace(
            "    schema: SALES\n",
            "    schema: SALES\n    externalConceptMappings:\n"
            "      - concept: corp:Order\n        relation: exact\n",
        )
        model, errors = _resolve(yaml_text)
        assert errors == []
        [m] = model.data_objects["Orders"].external_concept_mappings
        assert m.expanded_iri == CORP + "Order"

    def test_dimension(self) -> None:
        yaml_text = _BASE.replace(
            "    column: ID\n",
            "    column: ID\n    externalConceptMappings:\n"
            "      - concept: corp:OrderIdentifier\n        relation: exact\n",
        )
        model, errors = _resolve(yaml_text)
        assert errors == []
        [m] = model.dimensions["Order ID"].external_concept_mappings
        assert m.expanded_iri == CORP + "OrderIdentifier"

    def test_measure(self) -> None:
        model, errors = _resolve(_with_measure_mappings(self._ENTRY))
        assert errors == []
        assert model.measures["Revenue"].external_concept_mappings[0].expanded_iri == CORP + "Thing"

    def test_derived_metric(self) -> None:
        yaml_text = _BASE.replace(
            '    expression: "{[Revenue]} * 2"\n',
            '    expression: "{[Revenue]} * 2"\n    externalConceptMappings:\n'
            "      - concept: corp:DoubleRevenue\n        relation: exact\n",
        )
        model, errors = _resolve(yaml_text)
        assert errors == []
        [m] = model.metrics["Revenue Doubled"].external_concept_mappings
        assert m.expanded_iri == CORP + "DoubleRevenue"

    @pytest.mark.parametrize(
        "block",
        [
            "type: cumulative\nmeasure: Revenue\ntimeDimension: Order Month\n",
            "type: window\nmeasure: Revenue\nwindowFunction: rank\n",
            'type: period_over_period\nexpression: "{[Revenue]}"\n'
            "periodOverPeriod:\n  timeDimension: Order Month\n  offset: 1\n  offsetGrain: month\n",
        ],
        ids=["cumulative", "window", "period_over_period"],
    )
    def test_other_metric_types(self, block: str) -> None:
        indented = "\n".join(f"    {line}" for line in block.splitlines())
        yaml_text = _BASE + (
            f"  Running:\n{indented}\n    externalConceptMappings:\n"
            "      - concept: corp:Running\n        relation: exact\n"
        )
        model, errors = _resolve(yaml_text)
        assert errors == [], errors
        [m] = model.metrics["Running"].external_concept_mappings
        assert m.expanded_iri == CORP + "Running"

    def test_column_is_rejected(self) -> None:
        yaml_text = _BASE.replace(
            "        code: AMOUNT\n",
            "        code: AMOUNT\n        externalConceptMappings:\n"
            "          - concept: corp:Amount\n            relation: exact\n",
        )
        _, errors = _resolve(yaml_text)
        assert [(e.code, e.path) for e in errors] == [
            ("UNKNOWN_PROPERTY", "dataObjects.Orders.columns.Amount")
        ]

    def test_join_is_rejected(self) -> None:
        yaml_text = _BASE.replace(
            "dimensions:\n",
            "  Customers:\n    code: CUSTOMERS\n    columns:\n      ID: {code: ID}\n",
            1,
        ).replace(
            "    schema: SALES\n",
            "    schema: SALES\n    joins:\n      - joinTo: Customers\n        columnsFrom: [ID]\n"
            "        columnsTo: [ID]\n        externalConceptMappings: []\n",
        )
        yaml_text = yaml_text.replace(
            "      ID: {code: ID}\n", "      ID: {code: ID}\ndimensions:\n", 1
        )
        _, errors = _resolve(yaml_text)
        assert "UNKNOWN_PROPERTY" in _codes(errors)


# ──────────────────────────── invalid shapes ────────────────────────────


class TestPrefixErrors:
    def test_unknown_prefix(self) -> None:
        model, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: acme:Revenue\n    relation: exact\n"
            )
        )
        [err] = errors
        assert err.code == "UNKNOWN_ONTOLOGY_PREFIX"
        assert err.path == "measures.Revenue.externalConceptMappings[0]"
        assert err.span is not None
        assert {"corp", "fibo", "skos"} <= set(err.suggestions)
        # The measure itself still loads; only the mapping is dropped.
        assert model.measures["Revenue"].external_concept_mappings == []

    def test_invalid_prefix_name(self) -> None:
        yaml_text = _BASE.replace("    fibo:", '    "1bad": "https://x.example/"\n    fibo:')
        _, errors = _resolve(yaml_text)
        [err] = errors
        assert err.code == "INVALID_ONTOLOGY_PREFIX"
        assert err.path == "ontology.prefixes.1bad"

    @pytest.mark.parametrize("namespace", ["relative/path", "corp:X", "'has space'", "42"])
    def test_invalid_namespace(self, namespace: str) -> None:
        yaml_text = _BASE.replace("    fibo:", f"    bad: {namespace}\n    fibo:")
        _, errors = _resolve(yaml_text)
        assert _codes(errors) == ["INVALID_ONTOLOGY_PREFIX"]
        assert errors[0].path == "ontology.prefixes.bad"

    def test_rebinding_a_builtin_is_rejected(self) -> None:
        yaml_text = _BASE.replace(
            "    fibo:", '    skos: "https://example.com/not-skos#"\n    fibo:'
        )
        _, errors = _resolve(yaml_text)
        assert _codes(errors) == ["INVALID_ONTOLOGY_PREFIX"]
        assert "built in" in errors[0].message

    def test_bad_prefix_does_not_hide_the_good_ones(self) -> None:
        yaml_text = _BASE.replace("    fibo:", "    bad: nope\n    fibo:")
        model, errors = _resolve(yaml_text)
        assert _codes(errors) == ["INVALID_ONTOLOGY_PREFIX"]
        assert model.ontology is not None
        assert set(model.ontology.prefixes) == {"corp", "fibo"}

    def test_ontology_block_must_be_a_mapping(self) -> None:
        yaml_text = _BASE.replace(
            'ontology:\n  prefixes:\n    corp: "https://ontology.example.com/business/"\n'
            '    fibo: "https://spec.edmcouncil.org/fibo/ontology/"\n',
            "ontology: [corp]\n",
        )
        _, errors = _resolve(yaml_text)
        assert _codes(errors) == ["ONTOLOGY_PARSE_ERROR"]

    def test_prefixes_must_be_a_mapping(self) -> None:
        yaml_text = _BASE.replace(
            '  prefixes:\n    corp: "https://ontology.example.com/business/"\n'
            '    fibo: "https://spec.edmcouncil.org/fibo/ontology/"\n',
            "  prefixes: [corp]\n",
        )
        _, errors = _resolve(yaml_text)
        assert _codes(errors) == ["ONTOLOGY_PARSE_ERROR"]
        assert errors[0].path == "ontology.prefixes"

    def test_unknown_key_in_ontology_block(self) -> None:
        yaml_text = _BASE.replace("ontology:\n", "ontology:\n  imports: []\n")
        _, errors = _resolve(yaml_text)
        assert [(e.code, e.path) for e in errors] == [("UNKNOWN_PROPERTY", "ontology")]


class TestConceptIriErrors:
    @pytest.mark.parametrize(
        "concept",
        [
            "_:b0",
            "NetRevenue",
            "<https://ontology.example.com/business/NetRevenue>",
            "'corp:Net Revenue'",
            "'corp:'",
            "''",
        ],
        ids=["blank-node", "relative", "angle-brackets", "whitespace", "no-local-name", "empty"],
    )
    def test_invalid_concept(self, concept: str) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                f"externalConceptMappings:\n  - concept: {concept}\n    relation: exact\n"
            )
        )
        assert _codes(errors) == ["INVALID_CONCEPT_IRI"], errors
        assert errors[0].path == "measures.Revenue.externalConceptMappings[0]"
        assert errors[0].span is not None


class TestMappingShapeErrors:
    def test_missing_relation(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings("externalConceptMappings:\n  - concept: corp:NetRevenue\n")
        )
        [err] = errors
        assert err.code == "INVALID_CONCEPT_MAPPING"
        assert "relation" in err.message
        assert err.path == "measures.Revenue.externalConceptMappings[0]"

    def test_invalid_relation(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: same\n"
            )
        )
        assert _codes(errors) == ["INVALID_CONCEPT_MAPPING"]
        assert "relation" in errors[0].message

    def test_invalid_justification(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: exact\n"
                "    justification: guessed\n"
            )
        )
        assert _codes(errors) == ["INVALID_CONCEPT_MAPPING"]
        assert "justification" in errors[0].message

    @pytest.mark.parametrize("confidence", ["-0.1", "1.5"])
    def test_confidence_out_of_range(self, confidence: str) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: exact\n"
                f"    confidence: {confidence}\n"
            )
        )
        assert _codes(errors) == ["INVALID_CONCEPT_MAPPING"]
        assert "confidence" in errors[0].message

    def test_unknown_key_in_entry(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: exact\n"
                "    relatoin: exact\n"
            )
        )
        assert [(e.code, e.path) for e in errors] == [
            ("UNKNOWN_PROPERTY", "measures.Revenue.externalConceptMappings[0]")
        ]

    def test_expanded_iri_is_not_authorable(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n  - concept: corp:NetRevenue\n    relation: exact\n"
                "    expandedIri: https://x.example/\n"
            )
        )
        assert _codes(errors) == ["UNKNOWN_PROPERTY"]

    def test_entries_must_be_a_list(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings("externalConceptMappings:\n  concept: corp:NetRevenue\n")
        )
        assert _codes(errors) == ["INVALID_CONCEPT_MAPPING"]
        assert errors[0].path == "measures.Revenue.externalConceptMappings"

    def test_entry_must_be_a_mapping(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings("externalConceptMappings:\n  - corp:NetRevenue\n")
        )
        assert _codes(errors) == ["INVALID_CONCEPT_MAPPING"]
        assert errors[0].path == "measures.Revenue.externalConceptMappings[0]"


class TestDuplicatesAndConflicts:
    def test_duplicate_same_relation(self) -> None:
        model, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n"
                "  - concept: corp:NetRevenue\n    relation: exact\n"
                "  - concept: https://ontology.example.com/business/NetRevenue\n"
                "    relation: exact\n"
            )
        )
        [err] = errors
        assert err.code == "DUPLICATE_CONCEPT_MAPPING"
        assert err.path == "measures.Revenue.externalConceptMappings[1]"
        # The first occurrence is kept, the repeat dropped.
        assert len(model.measures["Revenue"].external_concept_mappings) == 1

    def test_conflicting_relations(self) -> None:
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n"
                "  - concept: corp:NetRevenue\n    relation: exact\n"
                "  - concept: corp:NetRevenue\n    relation: broader\n"
            )
        )
        [err] = errors
        assert err.code == "CONFLICTING_CONCEPT_MAPPING"
        assert "'exact'" in err.message and "'broader'" in err.message

    def test_each_problem_is_reported_once(self) -> None:
        """Three broken entries give three errors, one per entry, and none masks another."""
        _, errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n"
                "  - concept: acme:Revenue\n    relation: exact\n"
                "  - concept: corp:Revenue\n    relation: nope\n"
                "  - concept: corp:Revenue\n    relation: exact\n"
                "  - concept: corp:Revenue\n    relation: exact\n"
            )
        )
        assert _codes(errors) == [
            "UNKNOWN_ONTOLOGY_PREFIX",
            "INVALID_CONCEPT_MAPPING",
            "DUPLICATE_CONCEPT_MAPPING",
        ]


# ───────────────────── existing models and execution ────────────────────


class TestNoBehaviourChange:
    def test_model_without_mappings_loads_unchanged(self) -> None:
        yaml_text = _BASE.replace(
            'ontology:\n  prefixes:\n    corp: "https://ontology.example.com/business/"\n'
            '    fibo: "https://spec.edmcouncil.org/fibo/ontology/"\n',
            "",
        )
        model, errors = _resolve(yaml_text)
        assert errors == []
        assert model.ontology is None
        assert model.external_concept_mappings == []
        assert all(o.external_concept_mappings == [] for o in model.data_objects.values())
        assert all(d.external_concept_mappings == [] for d in model.dimensions.values())
        assert all(m.external_concept_mappings == [] for m in model.measures.values())
        assert all(m.external_concept_mappings == [] for m in model.metrics.values())

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "snowflake"])
    def test_sql_and_cache_key_are_identical_with_mappings(self, dialect: str) -> None:
        plain, plain_errors = _resolve(_BASE)
        mapped, mapped_errors = _resolve(
            _with_measure_mappings(
                "externalConceptMappings:\n"
                "  - concept: corp:NetRevenue\n    relation: exact\n"
                "  - concept: fibo:Revenue\n    relation: broader\n    confidence: 0.7\n"
            )
            + "externalConceptMappings:\n  - concept: corp:SalesModel\n    relation: exact\n"
        )
        assert plain_errors == [] and mapped_errors == []
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Month"], measures=["Revenue"]),
        )
        pipeline = CompilationPipeline()
        a = pipeline.compile(query, plain, dialect)
        b = pipeline.compile(query, mapped, dialect)
        assert a.sql == b.sql
        assert [w.code for w in a.warnings] == [w.code for w in b.warnings]
        key = build_cache_key(datasource="ds", model_id="m", dialect=dialect, sql=a.sql)
        assert key == build_cache_key(datasource="ds", model_id="m", dialect=dialect, sql=b.sql)


class TestExtendsMerge:
    def test_extension_prefixes_and_model_mappings_survive_merge(self) -> None:
        base_raw, _ = TrackedLoader().load_string(
            _BASE + "externalConceptMappings:\n  - concept: corp:Base\n    relation: exact\n"
        )
        extension = (
            "version: 1.0\n"
            "ontology:\n  prefixes:\n    gr: 'http://purl.org/goodrelations/v1#'\n"
            "measures:\n  Order Count:\n    aggregation: count\n"
            "    columns: [{dataObject: Orders, column: ID}]\n"
            "    externalConceptMappings:\n"
            "      - concept: gr:Offering\n        relation: related\n"
            "externalConceptMappings:\n"
            "  - concept: gr:BusinessEntity\n    relation: related\n"
        )
        merged, warnings = ExtendsMerger().merge_from_strings(base_raw, [extension])
        assert warnings == []
        model, result = ReferenceResolver().resolve(merged)
        assert result.errors == []
        assert model.ontology is not None
        assert set(model.ontology.prefixes) == {"corp", "fibo", "gr"}
        assert [m.concept for m in model.external_concept_mappings] == [
            "corp:Base",
            "gr:BusinessEntity",
        ]
        [m] = model.measures["Order Count"].external_concept_mappings
        assert m.expanded_iri == "http://purl.org/goodrelations/v1#Offering"

    def test_rebinding_a_prefix_in_an_extension_is_an_error(self) -> None:
        """A rebinding would silently expand the base file's mappings elsewhere."""
        base_raw, _ = TrackedLoader().load_string(
            _BASE + "externalConceptMappings:\n  - concept: corp:Base\n    relation: exact\n"
        )
        extension = "version: 1.0\nontology:\n  prefixes:\n    corp: 'https://other.example/'\n"
        with pytest.raises(MergeError) as exc:
            ExtendsMerger().merge_from_strings(base_raw, [extension])
        assert exc.value.code == "ONTOLOGY_PREFIX_CONFLICT"
        assert "corp" in exc.value.message

    def test_redeclaring_a_prefix_verbatim_in_an_extension_is_fine(self) -> None:
        base_raw, _ = TrackedLoader().load_string(_BASE)
        extension = f"version: 1.0\nontology:\n  prefixes:\n    corp: '{CORP}'\n"
        merged, warnings = ExtendsMerger().merge_from_strings(base_raw, [extension])
        assert warnings == []
        assert merged["ontology"]["prefixes"]["corp"] == CORP


class TestInheritsMerge:
    def test_child_prefixes_and_mappings_survive_inherits(self) -> None:
        parent_raw, _ = TrackedLoader().load_string(
            _BASE + "externalConceptMappings:\n  - concept: corp:Parent\n    relation: exact\n"
        )
        child_raw, _ = TrackedLoader().load_string(
            "version: 1.0\n"
            "ontology:\n  prefixes:\n    gr: 'http://purl.org/goodrelations/v1#'\n"
            "measures:\n  Order Count:\n    aggregation: count\n"
            "    columns: [{dataObject: Orders, column: ID}]\n"
            "    externalConceptMappings:\n"
            "      - concept: gr:Offering\n        relation: related\n"
            "externalConceptMappings:\n"
            "  - concept: gr:BusinessEntity\n    relation: related\n"
        )
        merged, warnings = ExtendsMerger().merge_from_strings(child_raw, inherits_raw=parent_raw)
        assert warnings == []
        model, result = ReferenceResolver().resolve(merged)
        assert result.errors == []
        assert model.ontology is not None
        assert set(model.ontology.prefixes) == {"corp", "fibo", "gr"}
        assert [m.concept for m in model.external_concept_mappings] == [
            "corp:Parent",
            "gr:BusinessEntity",
        ]
        [m] = model.measures["Order Count"].external_concept_mappings
        assert m.expanded_iri == "http://purl.org/goodrelations/v1#Offering"

    @pytest.mark.parametrize("bad_block", ["ontology: []\n", "ontology:\n  prefixes: [corp]\n"])
    def test_malformed_parent_ontology_reaches_the_resolver(self, bad_block: str) -> None:
        """The merger leaves a malformed block alone; the resolver reports it."""
        parent_raw, _ = TrackedLoader().load_string(
            _BASE.replace(
                'ontology:\n  prefixes:\n    corp: "https://ontology.example.com/business/"\n'
                '    fibo: "https://spec.edmcouncil.org/fibo/ontology/"\n',
                bad_block,
            )
        )
        child_raw, _ = TrackedLoader().load_string(
            f"version: 1.0\nontology:\n  prefixes:\n    corp: '{CORP}'\n"
        )
        merged, _ = ExtendsMerger().merge_from_strings(child_raw, inherits_raw=parent_raw)
        _, result = ReferenceResolver().resolve(merged)
        assert _codes(result.errors) == ["ONTOLOGY_PARSE_ERROR"]

    def test_malformed_base_ontology_with_extension_reaches_the_resolver(self) -> None:
        base_raw, _ = TrackedLoader().load_string(
            _BASE.replace(
                'ontology:\n  prefixes:\n    corp: "https://ontology.example.com/business/"\n'
                '    fibo: "https://spec.edmcouncil.org/fibo/ontology/"\n',
                "ontology: []\n",
            )
        )
        extension = f"version: 1.0\nontology:\n  prefixes:\n    corp: '{CORP}'\n"
        merged, _ = ExtendsMerger().merge_from_strings(base_raw, [extension])
        _, result = ReferenceResolver().resolve(merged)
        assert _codes(result.errors) == ["ONTOLOGY_PARSE_ERROR"]

    def test_child_rebinding_a_parent_prefix_is_an_error(self) -> None:
        parent_raw, _ = TrackedLoader().load_string(_BASE)
        child_raw, _ = TrackedLoader().load_string(
            "version: 1.0\nontology:\n  prefixes:\n    corp: 'https://other.example/'\n"
        )
        with pytest.raises(MergeError) as exc:
            ExtendsMerger().merge_from_strings(child_raw, inherits_raw=parent_raw)
        assert exc.value.code == "ONTOLOGY_PREFIX_CONFLICT"


# ────────────────────────────── JSON schema ─────────────────────────────


class TestJsonSchema:
    _DOC = {
        "version": 1.0,
        "ontology": {"prefixes": {"corp": CORP}},
        "dataObjects": {
            "O": {
                "code": "O",
                "database": "d",
                "schema": "s",
                "columns": {"C": {"code": "C", "abstractType": "string"}},
                "externalConceptMappings": [{"concept": "corp:Order", "relation": "exact"}],
            }
        },
        "dimensions": {
            "D": {
                "dataObject": "O",
                "column": "C",
                "externalConceptMappings": [{"concept": "corp:D", "relation": "close"}],
            }
        },
        "measures": {
            "M": {
                "aggregation": "count",
                "columns": [{"dataObject": "O", "column": "C"}],
                "externalConceptMappings": [
                    {
                        "concept": "corp:M",
                        "relation": "broader",
                        "justification": "curated",
                        "source": "s",
                        "ontologyVersion": "1",
                        "confidence": 0.5,
                        "comment": "c",
                    }
                ],
            }
        },
        "metrics": {
            "X": {
                "expression": "{[M]}",
                "externalConceptMappings": [{"concept": "corp:X", "relation": "narrower"}],
            }
        },
        "externalConceptMappings": [{"concept": "https://x.example/Model", "relation": "related"}],
    }

    def _errors(self, doc: dict) -> list[str]:
        return [e.message for e in _VALIDATOR.iter_errors(doc)]

    def test_full_document_validates(self) -> None:
        assert self._errors(self._DOC) == []

    def _mutated(self, path: list, value) -> dict:
        doc = json.loads(json.dumps(self._DOC))
        node = doc
        for key in path[:-1]:
            node = node[key]
        if value is _DELETE:
            del node[path[-1]]
        else:
            node[path[-1]] = value
        return doc

    def test_relation_is_required(self) -> None:
        doc = self._mutated(["metrics", "X", "externalConceptMappings", 0, "relation"], _DELETE)
        assert any("relation" in m for m in self._errors(doc))

    def test_unknown_relation_rejected(self) -> None:
        doc = self._mutated(["metrics", "X", "externalConceptMappings", 0, "relation"], "same")
        assert self._errors(doc)

    def test_confidence_bounds(self) -> None:
        doc = self._mutated(["measures", "M", "externalConceptMappings", 0, "confidence"], 1.5)
        assert self._errors(doc)

    def test_expanded_iri_rejected(self) -> None:
        doc = self._mutated(
            ["metrics", "X", "externalConceptMappings", 0, "expandedIri"], "https://x.example/"
        )
        assert self._errors(doc)

    def test_invalid_prefix_name_rejected(self) -> None:
        doc = self._mutated(["ontology", "prefixes"], {"1bad": CORP})
        assert self._errors(doc)

    def test_mappings_on_column_rejected(self) -> None:
        doc = self._mutated(
            ["dataObjects", "O", "columns", "C", "externalConceptMappings"],
            [{"concept": "corp:C", "relation": "exact"}],
        )
        assert self._errors(doc)


_DELETE = object()


# ──────────────────────────── model + helpers ───────────────────────────


class TestPydanticModel:
    def test_python_names_and_aliases(self) -> None:
        m = ExternalConceptMapping(concept="corp:X", relation="exact", ontology_version="1")
        assert m.relation is ExternalConceptRelation.EXACT
        assert ExternalConceptMapping.model_validate({"concept": "corp:X", "relation": "close"})
        dumped = m.model_dump(by_alias=True, exclude_none=True)
        assert dumped == {"concept": "corp:X", "relation": "exact", "ontologyVersion": "1"}

    def test_relation_required(self) -> None:
        with pytest.raises(ValidationError):
            ExternalConceptMapping(concept="corp:X")  # type: ignore[call-arg]

    @pytest.mark.parametrize("confidence", [-0.01, 1.01])
    def test_confidence_bounds(self, confidence: float) -> None:
        with pytest.raises(ValidationError):
            ExternalConceptMapping(concept="corp:X", relation="exact", confidence=confidence)


class TestConceptLinkHelpers:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("https://example.com/a", True),
            ("http://example.com", True),
            ("urn:isbn:0451450523", True),
            ("corp:NetRevenue", False),
            ("https://", False),
            ("https://exa mple.com/a", False),
            ("/relative", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_absolute_iri(self, value: object, expected: bool) -> None:
        assert is_absolute_iri(value) is expected

    def test_effective_prefixes_layers_declared_over_builtins(self) -> None:
        merged = effective_prefixes({"corp": CORP})
        assert merged["corp"] == CORP
        assert merged["skos"] == BUILTIN_PREFIXES["skos"]
        assert effective_prefixes(None) == dict(BUILTIN_PREFIXES)

    def test_expand_compact(self) -> None:
        assert expand_concept("corp:Net", {"corp": CORP}) == CORP + "Net"

    def test_expand_full_iri_verbatim(self) -> None:
        assert expand_concept("urn:x:y", {}) == "urn:x:y"

    def test_unknown_prefix_error_carries_suggestions(self) -> None:
        with pytest.raises(ConceptIriError) as exc:
            expand_concept("acme:X", {"corp": CORP})
        assert exc.value.code == "UNKNOWN_ONTOLOGY_PREFIX"
        assert exc.value.suggestions == ["corp"]
