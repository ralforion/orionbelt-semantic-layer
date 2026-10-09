"""Reaggregate metrics: the OBML surface (model, parser, schema, graph, lineage).

Compiled results are checked against hand-written SQL in
``tests/integration/correctness/test_reaggregate_reference.py``; here the SQL
shape, the warnings and the refusals.
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
from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.type_resolver import measure_yields_integers
from orionbelt.models.errors import ValidationResult
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import (
    _REAGGREGATE_FIELDS,
    Measure,
    Metric,
    MetricType,
    ReaggregateAggType,
    SemanticModel,
)
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
  Daily Revenue Share:
    expression: '{[Avg Daily Revenue]} / {[Revenue]}'
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

    @pytest.mark.parametrize(
        ("override", "alias"),
        [
            ({"cumulative_type": "sum"}, "cumulativeType"),
            ({"order_direction": "desc"}, "orderDirection"),
            ({"default_value": 0}, "defaultValue"),
        ],
    )
    def test_explicit_settings_of_other_types_refused(
        self, override: dict[str, Any], alias: str
    ) -> None:
        # Refused even when the value equals the default: it was written down
        # and has no effect on a reaggregate metric.
        with pytest.raises(ValidationError, match=f"must not have {alias}"):
            self._metric(**override)

    def test_defaults_of_other_types_are_not_refused(self) -> None:
        met = self._metric()
        assert met.cumulative_type.value == "sum"
        assert met.order_direction == "desc"

    def test_allowlist_names_real_fields(self) -> None:
        assert _REAGGREGATE_FIELDS.issubset(Metric.model_fields)

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
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "cumulativeType: sum",
                "METRIC_PARSE_ERROR",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "orderDirection: asc",
                "METRIC_PARSE_ERROR",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "defaultValue: 0",
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
    """Offered as a query choice."""

    @pytest.mark.parametrize("anchors", [[], ["Country"]])
    def test_offered(self, model: SemanticModel, anchors: list[str]) -> None:
        result = resolve_composables_for_anchors(model, anchors)
        offered = set(result.metrics) | set(result.cfl_metrics)
        assert {
            "Avg Revenue per Customer",
            "Avg Orders per Customer",
            "Avg Daily Revenue",
            "Daily Revenue Share",
        } <= offered


def _compile(model: SemanticModel, query: QueryObject) -> CompilationResult:
    return CompilationPipeline().compile(query, model, "duckdb")


def _refusal(model: SemanticModel, query: QueryObject) -> set[str]:
    with pytest.raises(ResolutionError) as exc_info:
        _compile(model, query)
    return {e.code for e in exc_info.value.errors}


class TestCompile:
    def test_two_stages_joined_back(self, model: SemanticModel) -> None:
        sql = _compile(
            model,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Country"], measures=["Revenue", "Avg Revenue per Customer"]
                )
            ),
        ).sql
        assert '"reagg_1_inner"' in sql
        assert 'AVG("reagg_1_inner"."Revenue")' in sql
        assert 'LEFT JOIN "reagg_1"' in sql
        # The placeholder the planner projected is not in the base CTE.
        base = sql.split('"reagg_1_inner" AS')[0]
        assert '"Avg Revenue per Customer"' not in base

    def test_shared_scan(self, model: SemanticModel) -> None:
        resolved = _with_metric(
            "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: max"
        )
        probe_model, result = _resolve(resolved)
        assert result.valid
        sql = _compile(
            probe_model,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Country"], measures=["Avg Revenue per Customer", "Probe"]
                )
            ),
        ).sql
        assert sql.count('_inner" AS (') == 1

    def test_per_already_in_query_warns(self, model: SemanticModel) -> None:
        result = _compile(
            model,
            QueryObject(
                select=QuerySelect(dimensions=["Customer"], measures=["Avg Revenue per Customer"])
            ),
        )
        assert "REAGGREGATE_NO_OP" in {w.code for w in result.warnings}

    def test_refused_under_rollup(self, model: SemanticModel) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Country"], measures=["Avg Revenue per Customer"]),
            grouping="rollup",
        )
        assert "REAGGREGATE_WITH_ROLLUP" in _refusal(model, query)

    def test_per_grain_beside_the_query_dimension_at_a_coarser_grain(
        self, model: SemanticModel
    ) -> None:
        """Stage 1 groups by month and day; the day column gets its own name."""
        result = _compile(
            model,
            QueryObject(
                select=QuerySelect(dimensions=["Order Date:month"], measures=["Avg Daily Revenue"])
            ),
        )
        inner = result.sql.split('"reagg_1_inner" AS (')[1].split("),")[0]
        assert "DATE_TRUNC('month'" in inner
        assert "DATE_TRUNC('day'" in inner
        assert 'AS "Order Date:day"' in inner
        assert "REAGGREGATE_NO_OP" not in {w.code for w in result.warnings}

    def test_per_grain_without_the_dimension_in_the_query(self, model: SemanticModel) -> None:
        sql = _compile(
            model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Avg Daily Revenue"])),
        ).sql
        inner = sql.split('"reagg_1_inner" AS (')[1].split("),")[0]
        assert "DATE_TRUNC('day'" in inner
        assert '"Order Date:day"' not in inner

    def test_declared_grain_under_a_coarser_query_grain(self) -> None:
        """A bare ``per`` name groups by the dimension's declared timeGrain."""
        probe_model, result = _resolve(
            _with_metric("type: reaggregate\nmeasure: Revenue\nper: [Order Date]\naggregation: avg")
        )
        assert result.valid
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Order Date:year"], measures=["Probe"])),
        ).sql
        inner = sql.split('"reagg_1_inner" AS (')[1].split("),")[0]
        assert "DATE_TRUNC('year'" in inner
        assert (
            'DATE_TRUNC(\'month\', "Orders"."ORDER_DATE") AS DATE) AS "Order Date:month"' in inner
        )

    def test_per_grain_already_in_the_query_is_a_no_op(self, model: SemanticModel) -> None:
        result = _compile(
            model,
            QueryObject(
                select=QuerySelect(dimensions=["Order Date:day"], measures=["Avg Daily Revenue"])
            ),
        )
        assert "REAGGREGATE_NO_OP" in {w.code for w in result.warnings}
        inner = result.sql.split('"reagg_1_inner" AS (')[1].split("),")[0]
        assert inner.count("DATE_TRUNC(") == 1

    def test_week_does_not_nest_in_month(self) -> None:
        """Weeks cross months, so a query by week still splits them per month."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: ['Order Date:month']\naggregation: avg"
            )
        )
        assert result.valid
        result_ = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Order Date:week"], measures=["Probe"])),
        )
        assert "REAGGREGATE_NO_OP" not in {w.code for w in result_.warnings}
        assert 'AS "Order Date:month"' in result_.sql

    @pytest.mark.parametrize("other", ["Total Revenue", "Unfiltered Revenue"])
    def test_combination_refused(self, model: SemanticModel, other: str) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Country"], measures=["Avg Revenue per Customer", other])
        )
        assert "REAGGREGATE_COMBINATION_NOT_SUPPORTED" in _refusal(model, query)

    def test_having_on_the_metric_filters_after_the_second_stage(
        self, model: SemanticModel
    ) -> None:
        sql = _compile(
            model,
            QueryObject(
                select=QuerySelect(dimensions=["Country"], measures=["Avg Revenue per Customer"]),
                having=[QueryFilter(field="Avg Revenue per Customer", op=">", value=10)],
            ),
        ).sql
        # Held back from the planner's query and applied over the final rows.
        assert "HAVING" not in sql
        assert 'WHERE "Avg Revenue per Customer" > 10' in sql

    def test_derived_metric_rebuilt_over_components(self) -> None:
        probe_model, result = _resolve(
            _with_metric("expression: '{[Avg Revenue per Customer]} / {[Revenue]}'")
        )
        assert result.valid, result.errors
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"])),
        ).sql
        # The component is read from a column of its own, never a selected one.
        assert (
            '"reagg_1"."Avg Revenue per Customer" / NULLIF("reagg_base"."_reagg_component_1", 0)'
            in sql
        )


JOINED_MODEL_YAML = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Country: {code: COUNTRY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer ID]
        columnsTo: [Customer ID]
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Segment: {code: SEGMENT, abstractType: string}
  Weather:
    code: WEATHER
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Station: {code: STATION, abstractType: string}
dimensions:
  Country: {dataObject: Orders, column: Country, resultType: string}
  Segment: {dataObject: Customers, column: Segment, resultType: string}
  Station: {dataObject: Weather, column: Station, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
metrics:
  Avg Revenue per Segment:
    type: reaggregate
    measure: Revenue
    per: [Segment]
    aggregation: avg
  Avg Revenue per Station:
    type: reaggregate
    measure: Revenue
    per: [Station]
    aggregation: avg
  Station Share:
    expression: '{[Avg Revenue per Station]} / {[Revenue]}'
"""


@pytest.fixture(scope="module")
def joined_model() -> SemanticModel:
    resolved, result = _resolve(JOINED_MODEL_YAML)
    assert result.valid, result.errors
    return resolved


class TestPerDimensionObjects:
    """A ``per`` dimension's table counts wherever the query's own tables do."""

    def test_cache_sees_the_table_per_joins(self, joined_model: SemanticModel) -> None:
        result = _compile(
            joined_model,
            QueryObject(
                select=QuerySelect(dimensions=["Country"], measures=["Avg Revenue per Segment"])
            ),
        )
        assert "CUSTOMERS" in result.sql
        assert set(result.physical_tables) == {
            "WAREHOUSE.PUBLIC.ORDERS",
            "WAREHOUSE.PUBLIC.CUSTOMERS",
        }

    @pytest.mark.parametrize("anchors", [[], ["Country"]])
    def test_unreachable_per_not_offered(
        self, joined_model: SemanticModel, anchors: list[str]
    ) -> None:
        result = resolve_composables_for_anchors(joined_model, anchors)
        offered = set(result.metrics) | set(result.cfl_metrics)
        assert "Avg Revenue per Segment" in offered
        assert "Avg Revenue per Station" not in offered
        # The same holds through a derived metric over it.
        assert "Station Share" not in offered


_TYPED_MODEL_YAML = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: D
    schema: S
    columns:
      Units: {code: UNITS, abstractType: int}
      Price: {code: PRICE, abstractType: float}
"""


@pytest.fixture(scope="module")
def typed_model() -> SemanticModel:
    resolved, result = _resolve(_TYPED_MODEL_YAML)
    assert result.valid, result.errors
    return resolved


class TestMeasureYieldsIntegers:
    """Which first-stage values take the exact-integer second-stage AVG."""

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"columns": [{"dataObject": "T", "column": "Units"}], "aggregation": "count"}, True),
            (
                {
                    "columns": [{"dataObject": "T", "column": "Units"}],
                    "aggregation": "sum",
                    "resultType": "int",
                },
                True,
            ),
            ({"columns": [{"dataObject": "T", "column": "Units"}], "aggregation": "max"}, True),
            ({"columns": [{"dataObject": "T", "column": "Units"}], "aggregation": "mode"}, True),
            ({"columns": [{"dataObject": "T", "column": "Price"}], "aggregation": "max"}, False),
            ({"columns": [{"dataObject": "T", "column": "Units"}], "aggregation": "median"}, False),
            (
                {
                    "columns": [{"dataObject": "T", "column": "Units"}],
                    "aggregation": "min",
                    "defaultValue": 0.5,
                },
                False,
            ),
            ({"expression": "{[T].[Units]} * 2", "aggregation": "max", "resultType": "int"}, True),
            ({"expression": "{[T].[Units]} * 2", "aggregation": "max"}, False),
        ],
    )
    def test_answer(
        self, typed_model: SemanticModel, fields: dict[str, Any], expected: bool
    ) -> None:
        measure = Measure.model_validate({"name": "M", **fields})
        assert measure_yields_integers(measure, typed_model.settings, typed_model) is expected


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
