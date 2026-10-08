"""Reaggregate metrics: the OBML surface (model, parser, schema, graph, lineage).

The SQL lowering is not built yet, so a query selecting one is refused with
``REAGGREGATE_NOT_SUPPORTED`` rather than compiled through the derived path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError
from rdflib import Literal, URIRef
from rdflib.namespace import RDF

from orionbelt.compiler.composability import resolve_composables_for_anchors
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.errors import ValidationResult
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import Metric, MetricType, ReaggregateAggType, SemanticModel
from orionbelt.obsl.exporter import export_obsl
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator
from orionbelt.service.lineage import LineageBuilder

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "obml-schema.json").read_text()
)

MODEL_YAML = """\
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
      Order Date:
        code: ORDER_DATE
        abstractType: date
      Customer:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month
  Customer:
    dataObject: Orders
    column: Customer
    resultType: string
  Country:
    dataObject: Orders
    column: Country
    resultType: string

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    total: true
  Region Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    grain:
      mode: FIXED
      include: [Country]
  Unfiltered Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    filterContext:
      mode: FIXED

metrics:
  Avg Revenue per Customer:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: avg
  Avg Orders per Customer:
    type: reaggregate
    measure: Orders Count
    per: [Customer]
    aggregation: avg
  Avg Daily Revenue:
    type: reaggregate
    measure: Revenue
    per: ['Order Date:day']
    aggregation: avg
"""


def _resolve(yaml_text: str) -> tuple[SemanticModel, ValidationResult]:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    return ReferenceResolver().resolve(raw, source_map)


def _with_metric(body: str) -> str:
    """The base model with one extra metric named ``Probe``."""
    return MODEL_YAML + "  Probe:\n" + "".join(f"    {line}\n" for line in body.splitlines())


def _codes(yaml_text: str) -> set[str]:
    _model, result = _resolve(yaml_text)
    return {e.code for e in result.errors}


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    resolved, result = _resolve(MODEL_YAML)
    assert result.valid, result.errors
    return resolved


class TestParse:
    def test_fields(self, model: SemanticModel) -> None:
        met = model.metrics["Avg Revenue per Customer"]
        assert met.type is MetricType.REAGGREGATE
        assert met.measure == "Revenue"
        assert met.per == ["Customer"]
        assert met.aggregation is ReaggregateAggType.AVG

    def test_synthesized_count_is_a_valid_measure(self, model: SemanticModel) -> None:
        assert model.metrics["Avg Orders per Customer"].measure == "Orders Count"

    def test_per_with_time_grain(self, model: SemanticModel) -> None:
        assert model.metrics["Avg Daily Revenue"].per == ["Order Date:day"]


class TestMetricValidation:
    def _metric(self, **kwargs: Any) -> Metric:
        base: dict[str, Any] = {
            "name": "M",
            "type": "reaggregate",
            "measure": "Revenue",
            "per": ["Customer"],
            "aggregation": "avg",
        }
        return Metric(**{**base, **kwargs})

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"measure": None}, "require 'measure'"),
            ({"per": []}, "at least one 'per'"),
            ({"per": ["Customer", "Customer"]}, "must be unique"),
            ({"aggregation": None}, "require 'aggregation'"),
            ({"aggregation": "median"}, "aggregation"),
            ({"expression": "{[Revenue]}"}, "must not have expression"),
            ({"time_dimension": "Order Date", "window": 3}, "timeDimension, window"),
            ({"partition_by": ["Country"]}, "must not have partitionBy"),
        ],
    )
    def test_refused(self, override: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            self._metric(**override)

    def test_per_only_on_reaggregate(self) -> None:
        with pytest.raises(ValidationError, match="only valid on reaggregate"):
            Metric(name="D", expression="{[Revenue]}", per=["Customer"])

    def test_aggregation_only_on_reaggregate(self) -> None:
        with pytest.raises(ValidationError, match="only valid on reaggregate"):
            Metric(name="D", expression="{[Revenue]}", aggregation="avg")


class TestReferences:
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            (
                "type: reaggregate\nmeasure: Nope\nper: [Customer]\naggregation: avg",
                "UNKNOWN_MEASURE",
            ),
            (
                "type: reaggregate\nmeasure: Avg Daily Revenue\nper: [Customer]\naggregation: max",
                "REAGGREGATE_MEASURE_ONLY",
            ),
            (
                "type: reaggregate\nmeasure: Total Revenue\nper: [Customer]\naggregation: avg",
                "REAGGREGATE_INNER_GRAIN",
            ),
            (
                "type: reaggregate\nmeasure: Region Revenue\nper: [Customer]\naggregation: avg",
                "REAGGREGATE_INNER_GRAIN",
            ),
            (
                "type: reaggregate\nmeasure: Unfiltered Revenue\nper: [Customer]\naggregation: avg",
                "REAGGREGATE_INNER_FILTER_CONTEXT",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Custmer]\naggregation: avg",
                "REAGGREGATE_UNKNOWN_DIMENSION",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: ['Order Date:fortnight']\n"
                "aggregation: avg",
                "REAGGREGATE_INVALID_PER",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "partitionBy: [Country]",
                "METRIC_PARSE_ERROR",
            ),
        ],
    )
    def test_refused(self, body: str, code: str) -> None:
        assert code in _codes(_with_metric(body))

    def test_unknown_dimension_suggests(self) -> None:
        _model, result = _resolve(
            _with_metric("type: reaggregate\nmeasure: Revenue\nper: [Custmer]\naggregation: avg")
        )
        err = next(e for e in result.errors if e.code == "REAGGREGATE_UNKNOWN_DIMENSION")
        assert "Customer" in err.suggestions


class TestStrayFields:
    """``per`` / ``aggregation`` on another metric type is refused, not dropped."""

    @pytest.mark.parametrize(
        "body",
        [
            "expression: '{[Revenue]}'\nper: [Customer]",
            "expression: '{[Revenue]}'\naggregation: avg",
            "type: cumulative\nmeasure: Revenue\ntimeDimension: Order Date\nper: [Customer]",
        ],
    )
    def test_refused(self, body: str) -> None:
        _model, result = _resolve(_with_metric(body))
        assert any(
            e.code == "METRIC_PARSE_ERROR" and "only valid on reaggregate" in e.message
            for e in result.errors
        )


class TestPerGrain:
    """A ``per`` grain obeys the same column and resultType rules as a query's."""

    def _validator_codes(self, per: str) -> set[str]:
        model, result = _resolve(
            _with_metric(f"type: reaggregate\nmeasure: Revenue\nper: ['{per}']\naggregation: avg")
        )
        assert result.valid, result.errors
        return {e.code for e in SemanticValidator().validate(model)}

    def test_fixture_is_clean(self, model: SemanticModel) -> None:
        assert SemanticValidator().validate(model) == []

    def test_grain_on_a_string_column(self) -> None:
        assert "TIME_GRAIN_ON_NON_TEMPORAL" in self._validator_codes("Customer:day")

    def test_grain_finer_than_the_result_type(self) -> None:
        assert "RESULT_TYPE_LOSES_GRAIN" in self._validator_codes("Order Date:hour")

    def test_invalid_grain_reported_once_by_the_parser(self) -> None:
        model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: ['Order Date:fortnight']\n"
                "aggregation: avg"
            )
        )
        assert {e.code for e in result.errors} == {"REAGGREGATE_INVALID_PER"}
        assert SemanticValidator().validate(model) == []


class TestComposability:
    """Not offered as a query choice while every query selecting it is refused."""

    @pytest.mark.parametrize("anchors", [[], ["Country"]])
    def test_excluded(self, model: SemanticModel, anchors: list[str]) -> None:
        result = resolve_composables_for_anchors(model, anchors)
        offered = set(result.metrics) | set(result.cfl_metrics)
        assert not offered & {
            "Avg Revenue per Customer",
            "Avg Orders per Customer",
            "Avg Daily Revenue",
        }
        assert "Revenue" in result.measures


class TestCompile:
    def test_query_is_refused_until_lowered(self, model: SemanticModel) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Country"], measures=["Avg Revenue per Customer"])
        )
        with pytest.raises(ResolutionError) as exc_info:
            CompilationPipeline().compile(query, model, "duckdb")
        assert any(e.code == "REAGGREGATE_NOT_SUPPORTED" for e in exc_info.value.errors)


class TestJsonSchema:
    def _validate(self, metric: dict[str, Any]) -> list[str]:
        doc = {
            "version": 1.0,
            "dataObjects": {
                "O": {
                    "code": "O",
                    "database": "d",
                    "schema": "s",
                    "columns": {"C": {"code": "C", "abstractType": "string"}},
                }
            },
            "metrics": {"M": metric},
        }
        validator = jsonschema.Draft7Validator(_SCHEMA)
        return [e.message for e in validator.iter_errors(doc)]

    def test_valid(self) -> None:
        metric = {"type": "reaggregate", "measure": "R", "per": ["C"], "aggregation": "avg"}
        assert self._validate(metric) == []

    def test_requires_per_and_aggregation(self) -> None:
        assert self._validate({"type": "reaggregate", "measure": "R"})

    def test_per_refused_on_derived(self) -> None:
        assert self._validate({"expression": "{[R]}", "per": ["C"]})


class TestGraph:
    def test_rdf_triples(self, model: SemanticModel) -> None:
        g = export_obsl(model, "m")
        obsl = "https://ralforion.com/ns/obsl#"
        subjects = set(g.subjects(RDF.type, URIRef(obsl + "ReaggregateMetric")))
        assert len(subjects) == 3
        met = next(s for s in subjects if (s, URIRef(obsl + "per"), Literal("Order Date:day")) in g)
        assert (met, URIRef(obsl + "aggregation"), Literal("avg")) in g

    def test_lineage_has_per_edges(self, model: SemanticModel) -> None:
        lineage = LineageBuilder(model).metric("Avg Daily Revenue")
        labels = {
            (next(n.name for n in lineage.nodes if n.id == e.source), e.label)
            for e in lineage.edges
        }
        assert ("Order Date", "per") in labels
        assert ("Revenue", None) in labels
