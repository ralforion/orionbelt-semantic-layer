"""Reaggregate metrics: the OBML surface (model, parser, schema, graph, lineage).

Compiled results are checked against hand-written SQL in
``tests/integration/correctness/test_reaggregate_reference.py``; here the SQL
shape, the warnings and the refusals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS

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
            ({"aggregation": "mode"}, "aggregation"),
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

    def test_having_only_on_reaggregate(self) -> None:
        with pytest.raises(ValidationError, match="only valid on reaggregate"):
            Metric(
                name="D",
                expression="{[Revenue]}",
                having=[{"field": "Revenue", "op": ">", "value": 1}],
            )

    @pytest.mark.parametrize(
        ("op", "message"),
        [("approx", "unknown operator 'approx'"), ("exists", "not allowed")],
    )
    def test_having_operator_checked(self, op: str, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            self._metric(having=[{"field": "Revenue", "op": op, "value": 1}])

    def test_having_text(self) -> None:
        met = self._metric(
            having=[
                {"field": "Orders Count", "op": ">", "value": 5},
                {"field": "Revenue", "op": "is_not_null"},
            ]
        )
        assert [h.text for h in met.having] == ["Orders Count > 5", "Revenue is_not_null"]


class TestReferences:
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            (
                "type: reaggregate\nmeasure: Nope\nper: [Customer]\naggregation: avg",
                "UNKNOWN_MEASURE",
            ),
            (
                "type: reaggregate\nmeasure: Daily Revenue Share\nper: [Customer]\n"
                "aggregation: max",
                "REAGGREGATE_MEASURE_ONLY",
            ),
            (
                "type: reaggregate\nmeasure: Probe\nper: [Customer]\naggregation: max",
                "REAGGREGATE_CYCLE",
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
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Avg Daily Revenue, op: '>', value: 1}]",
                "REAGGREGATE_HAVING_MEASURE_ONLY",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Probe, op: '>', value: 1}]",
                "REAGGREGATE_HAVING_MEASURE_ONLY",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Revenu, op: '>', value: 1}]",
                "UNKNOWN_MEASURE",
            ),
            (
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Revenue, op: '>', vale: 1}]",
                "METRIC_PARSE_ERROR",
            ),
        ],
    )
    def test_refused(self, body: str, code: str) -> None:
        assert code in _codes(_with_metric(body))

    def test_over_a_reaggregate_metric(self) -> None:
        body = "type: reaggregate\nmeasure: Avg Daily Revenue\nper: [Customer]\naggregation: max"
        assert _codes(_with_metric(body)) == set()

    def test_cycle_named_once_per_metric_on_it(self) -> None:
        yaml_text = _with_metric(
            "type: reaggregate\nmeasure: Other\nper: [Customer]\naggregation: max"
        ) + (
            "  Other:\n    type: reaggregate\n    measure: Probe\n"
            "    per: [Country]\n    aggregation: avg\n"
        )
        _model, result = _resolve(yaml_text)
        messages = sorted(e.message for e in result.errors if e.code == "REAGGREGATE_CYCLE")
        assert messages == [
            "Reaggregate metrics reference each other in a cycle: Other -> Probe -> Other",
            "Reaggregate metrics reference each other in a cycle: Probe -> Other -> Probe",
        ]

    @pytest.mark.parametrize("field", ["Revenue", "Orders Count"])
    def test_having_on_a_measure(self, field: str) -> None:
        body = (
            "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
            f"having: [{{field: {field}, op: '>', value: 1}}]"
        )
        assert _codes(_with_metric(body)) == set()

    def test_unknown_having_measure_suggests(self) -> None:
        _model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Revenu, op: '>', value: 1}]"
            )
        )
        err = next(e for e in result.errors if e.code == "UNKNOWN_MEASURE")
        assert err.path == "metrics.Probe.having"
        assert "Revenue" in err.suggestions

    _AVERAGES = (
        "  Avg Price:\n    columns: [{dataObject: Orders, column: Amount}]\n    aggregation: avg\n"
    )

    @pytest.mark.parametrize(
        ("measure", "chain"),
        [
            ("Avg Price", "which is itself an average"),
            ("Avg Revenue per Customer", "which is itself an average"),
            ("Peak Avg", "average 'Avg Revenue per Customer' (Peak Avg -> Avg Revenue"),
            ("Peak Price", "average 'Avg Price' (Peak Price -> Avg Price)"),
            ("Middle Avg", "average 'Avg Revenue per Customer' (Middle Avg -> Avg Revenue"),
        ],
    )
    def test_average_of_averages_refused(self, measure: str, chain: str) -> None:
        """``avg`` over an average, directly or through a ``min`` / ``max`` /
        ``median`` stage, which picks one of the averages below it (or the mean
        of two)."""
        yaml_text = _with_metric(
            f"type: reaggregate\nmeasure: {measure}\nper: [Country]\naggregation: avg"
        ).replace("measures:\n", "measures:\n" + self._AVERAGES, 1) + (
            "  Peak Avg:\n    type: reaggregate\n    measure: Avg Revenue per Customer\n"
            "    per: [Order Date]\n    aggregation: max\n"
            "  Peak Price:\n    type: reaggregate\n    measure: Avg Price\n"
            "    per: [Customer]\n    aggregation: min\n"
            "  Middle Avg:\n    type: reaggregate\n    measure: Avg Revenue per Customer\n"
            "    per: [Order Date]\n    aggregation: median\n"
        )
        _model, result = _resolve(yaml_text)
        errors = [e for e in result.errors if e.code == "REAGGREGATE_AVG_OF_AVG"]
        assert [e.path for e in errors] == ["metrics.Probe.aggregation"]
        assert chain in errors[0].message

    @pytest.mark.parametrize("aggregation", ["sum", "min", "max", "count", "median"])
    def test_other_aggregations_of_an_average_allowed(self, aggregation: str) -> None:
        body = (
            f"type: reaggregate\nmeasure: Avg Revenue per Customer\nper: [Country]\n"
            f"aggregation: {aggregation}"
        )
        assert _codes(_with_metric(body)) == set()

    @pytest.mark.parametrize("inner", ["sum", "count"])
    def test_average_over_a_sum_or_count_of_averages_allowed(self, inner: str) -> None:
        """A sum or count of averages is no longer an average."""
        yaml_text = _with_metric(
            "type: reaggregate\nmeasure: Inner\nper: [Country]\naggregation: avg"
        ) + (
            f"  Inner:\n    type: reaggregate\n    measure: Avg Revenue per Customer\n"
            f"    per: [Order Date]\n    aggregation: {inner}\n"
        )
        assert _codes(yaml_text) == set()

    def test_unknown_dimension_suggests(self) -> None:
        _model, result = _resolve(
            _with_metric("type: reaggregate\nmeasure: Revenue\nper: [Custmer]\naggregation: avg")
        )
        err = next(e for e in result.errors if e.code == "REAGGREGATE_UNKNOWN_DIMENSION")
        assert "Customer" in err.suggestions


class TestStrayFields:
    """``per`` / ``aggregation`` / ``having`` on another metric type is refused, not dropped."""

    @pytest.mark.parametrize(
        "body",
        [
            "expression: '{[Revenue]}'\nper: [Customer]",
            "expression: '{[Revenue]}'\naggregation: avg",
            "type: cumulative\nmeasure: Revenue\ntimeDimension: Order Date\nper: [Customer]",
            "expression: '{[Revenue]}'\nhaving: [{field: Revenue, op: '>', value: 1}]",
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

    @pytest.mark.parametrize("dimensions", [["Country"], []])
    def test_two_per_buckets_of_one_date_get_their_own_columns(self, dimensions: list[str]) -> None:
        """Without the date in the query, its two ``per`` buckets still collide."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\n"
                "per: ['Order Date:month', 'Order Date:week']\naggregation: avg"
            )
        )
        assert result.valid
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=dimensions, measures=["Probe"])),
        ).sql
        inner = sql.split('"reagg_1_inner" AS (')[1].split("),")[0]
        assert 'AS "Order Date:month"' in inner
        assert 'AS "Order Date:week"' in inner
        assert 'AS "Order Date",' not in inner

    def test_per_entries_naming_one_bucket_group_once(self) -> None:
        """``Order Date`` is declared at month, so both entries are the month."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\n"
                "per: ['Order Date', 'Order Date:month']\naggregation: avg"
            )
        )
        assert result.valid
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"])),
        ).sql
        inner = sql.split('"reagg_1_inner" AS (')[1].split("),")[0]
        assert inner.count("DATE_TRUNC('month'") == 1
        assert 'AS "Order Date"' in inner

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

    @pytest.mark.parametrize(
        ("other", "wrapper_cte"),
        [("Total Revenue", '"base" AS ('), ("Unfiltered Revenue", '"main" AS (')],
    )
    def test_beside_a_wrapped_measure(
        self, model: SemanticModel, other: str, wrapper_cte: str
    ) -> None:
        """The other wrapper runs first; the reaggregate pass wraps what it built."""
        sql = _compile(
            model,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Country"], measures=["Avg Revenue per Customer", other]
                )
            ),
        ).sql
        assert sql.index(wrapper_cte) < sql.index('"reagg_base" AS (')
        assert f'"reagg_base"."{other}" AS "{other}"' in sql

    @pytest.mark.parametrize(
        ("inner", "wrapper"),
        [
            ("Total Revenue", "OVER ()"),
            ("Region Revenue", 'OVER (PARTITION BY "Country")'),
            ("Unfiltered Revenue", '"reagg_1_inner_2_fc_0" AS ('),
        ],
    )
    def test_over_a_wrapped_measure(self, inner: str, wrapper: str) -> None:
        """The measure's own wrapper runs in stage 1, the scan at the query grain
        plus ``per``; the outer query never computes the measure."""
        probe_model, result = _resolve(
            _with_metric(f"type: reaggregate\nmeasure: {inner}\nper: [Customer]\naggregation: avg")
        )
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"]))
        sql = _compile(probe_model, query).sql
        stage_one = sql.index('"reagg_1_inner" AS (')
        assert sql.count(wrapper) == 1
        assert stage_one < sql.index(wrapper) < sql.index('"reagg_1" AS (')

    def test_stage_one_ctes_named_apart_from_the_outer_query(self) -> None:
        """Both the outer query and the first stage have a total, so both have a
        ``base`` CTE; the nested one must not share the name (Snowflake reads a
        nested reference by the outer CTE's name)."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Total Revenue\nper: [Customer]\naggregation: avg"
            )
        )
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(dimensions=["Country"], measures=["Probe", "Total Revenue"])
        )
        sql = _compile(probe_model, query).sql
        assert sql.count('"base" AS (') == 1
        assert '"reagg_1_inner_1_base" AS (' in sql
        assert 'FROM "reagg_1_inner_1_base" AS "base"' in sql

    def test_over_a_reaggregate_metric_nests_its_stages(self) -> None:
        """The inner metric is the first stage, planned as a query of its own,
        so its two stages run inside it, named under it."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Avg Revenue per Customer\n"
                "per: ['Order Date:month']\naggregation: max"
            )
        )
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"]))
        sql = _compile(probe_model, query).sql
        positions = [
            sql.index(f'"{cte}" AS (')
            for cte in ("reagg_1_inner_2_reagg_1_inner", "reagg_1_inner_3_reagg_1", "reagg_1")
        ]
        assert positions == sorted(positions)
        assert sql.count("AVG(") == 1
        assert sql.count("MAX(") == 1 + sql.count("MAX(CAST(NULL")

    @pytest.mark.parametrize("dialect", ["postgres", "snowflake"])
    def test_stage_names_stay_short_and_unique_at_any_depth(self, dialect: str) -> None:
        """Each stage nests the next one's CTEs; a name that grew per stage
        passed PostgreSQL's 63 bytes at four, truncated to a sibling's."""
        chain = "".join(
            f"  Stage {i}:\n    type: reaggregate\n"
            f"    measure: {'Revenue' if i == 1 else f'Stage {i - 1}'}\n"
            "    per: [Customer]\n    aggregation: max\n"
            for i in range(1, 7)
        )
        probe_model, result = _resolve(MODEL_YAML + chain)
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Stage 6"]))
        sql = CompilationPipeline().compile(query, probe_model, dialect).sql
        names = re.findall(r'"([^"]+)" AS \(', sql)
        assert len(names) == 18
        assert len(set(names)) == len(names)
        assert max(len(name) for name in names) <= 32

    def test_untyped_placeholder_takes_the_source_column_type(self) -> None:
        """A ``min`` over a ``max`` measure declares no type anywhere; an
        untyped NULL is text on Postgres, which ``* 2`` does not bind to."""
        yaml_text = MODEL_YAML.replace(
            "metrics:\n",
            "  Largest Order:\n"
            "    columns:\n"
            "      - dataObject: Orders\n"
            "        column: Amount\n"
            "    aggregation: max\n"
            "metrics:\n"
            "  Smallest Peak:\n"
            "    type: reaggregate\n"
            "    measure: Largest Order\n"
            "    per: [Customer]\n"
            "    aggregation: min\n"
            "  Smallest Peak Doubled:\n"
            "    expression: '{[Smallest Peak]} * 2'\n",
        )
        probe_model, result = _resolve(yaml_text)
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"], measures=["Smallest Peak Doubled", "Total Revenue"]
            )
        )
        sql = CompilationPipeline().compile(query, probe_model, "postgres").sql
        assert "MAX(NULL)" not in sql
        assert "MAX(CAST(NULL AS FLOAT))" in sql

    def test_untyped_nested_placeholder_takes_the_source_column_type(self) -> None:
        """A ``max`` over a ``min`` over a ``max`` measure declares no type at
        any stage; the placeholder still takes the column's."""
        yaml_text = MODEL_YAML.replace(
            "metrics:\n",
            "  Largest Order:\n"
            "    columns:\n"
            "      - dataObject: Orders\n"
            "        column: Amount\n"
            "    aggregation: max\n"
            "metrics:\n"
            "  Smallest Peak:\n"
            "    type: reaggregate\n"
            "    measure: Largest Order\n"
            "    per: [Customer]\n"
            "    aggregation: min\n"
            "  Daily Smallest Peak:\n"
            "    type: reaggregate\n"
            "    measure: Smallest Peak\n"
            "    per: ['Order Date:day']\n"
            "    aggregation: max\n"
            "  Daily Smallest Peak Doubled:\n"
            "    expression: '{[Daily Smallest Peak]} * 2'\n",
        )
        probe_model, result = _resolve(yaml_text)
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country"], measures=["Daily Smallest Peak Doubled", "Total Revenue"]
            )
        )
        sql = CompilationPipeline().compile(query, probe_model, "postgres").sql
        assert "MAX(NULL)" not in sql
        assert "MAX(CAST(NULL AS FLOAT))" in sql

    @pytest.mark.parametrize("other", ["Total Revenue", "Unfiltered Revenue"])
    def test_formula_over_a_wrapped_component_refused(self, other: str) -> None:
        """The formula's components are read before the other wrapper runs."""
        probe_model, result = _resolve(
            _with_metric(f"expression: '{{[Avg Revenue per Customer]}} / {{[{other}]}}'")
        )
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"]))
        assert "REAGGREGATE_COMBINATION_NOT_SUPPORTED" in _refusal(probe_model, query)

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

    @staticmethod
    def _having_model(*metrics: tuple[str, str, str]) -> SemanticModel:
        """The base model plus reaggregate metrics of Revenue per Customer, each
        as (name, aggregation, having flow list)."""
        yaml_text = MODEL_YAML + "".join(
            f"  {name}:\n    type: reaggregate\n    measure: Revenue\n    per: [Customer]\n"
            f"    aggregation: {agg}\n" + (f"    having: {having}\n" if having else "")
            for name, agg, having in metrics
        )
        resolved, result = _resolve(yaml_text)
        assert result.valid, result.errors
        return resolved

    def test_having_is_the_first_stage_having(self) -> None:
        having_model = self._having_model(
            ("Avg Repeat", "avg", "[{field: Orders Count, op: '>', value: 2}]")
        )
        sql = _compile(
            having_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Avg Repeat"])),
        ).sql
        inner = sql.split('"reagg_1_inner" AS (')[1].split('"reagg_1" AS (')[0]
        assert re.search(r"HAVING .*COUNT.* > 2", inner)
        # The condition's measure is computed for the HAVING, not projected.
        assert '"Orders Count"' not in inner
        assert 'AVG("reagg_1_inner"."Revenue")' in sql

    def test_metrics_share_a_scan_only_with_the_same_having(self) -> None:
        repeat = "[{field: Orders Count, op: '>', value: 2}]"
        having_model = self._having_model(
            ("Avg Repeat", "avg", repeat),
            ("Max Repeat", "max", repeat),
            ("Avg Big", "avg", "[{field: Revenue, op: '>', value: 100}]"),
        )
        sql = _compile(
            having_model,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Country"],
                    measures=["Avg Repeat", "Max Repeat", "Avg Big", "Avg Revenue per Customer"],
                )
            ),
        ).sql
        assert sql.count('_inner" AS (') == 3

    def test_count_with_having_reads_zero_for_an_emptied_group(self) -> None:
        having_model = self._having_model(
            ("Repeat Customers", "count", "[{field: Orders Count, op: '>', value: 2}]"),
            ("Avg Repeat", "avg", "[{field: Orders Count, op: '>', value: 2}]"),
        )
        sql = _compile(
            having_model,
            QueryObject(
                select=QuerySelect(
                    dimensions=["Country"], measures=["Repeat Customers", "Avg Repeat"]
                )
            ),
        ).sql
        assert (
            'COALESCE("reagg_1"."Repeat Customers", CAST(0 AS BIGINT)) AS "Repeat Customers"' in sql
        )
        assert '"reagg_1"."Avg Repeat" AS "Avg Repeat"' in sql

    def test_count_fallback_takes_the_declared_type(self) -> None:
        """A text count compared with an integer 0 is refused by PostgreSQL and DuckDB."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: count\n"
                "dataType: string\nhaving: [{field: Orders Count, op: '>', value: 2}]"
            )
        )
        assert result.valid, result.errors
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"])),
        ).sql
        assert 'COALESCE("reagg_1"."Probe", CAST(0 AS VARCHAR)) AS "Probe"' in sql

    def test_median_is_the_dialect_median_uncast(self) -> None:
        """As a ``median`` measure: a default decimal would round the midpoint."""
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: median"
            )
        )
        assert result.valid, result.errors
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"])),
        ).sql
        assert 'MEDIAN(CAST("reagg_1_inner"."Revenue" AS DOUBLE)) AS "Probe"' in sql

    def test_count_without_having_is_read_as_is(self) -> None:
        having_model = self._having_model(("Customers", "count", ""))
        sql = _compile(
            having_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Customers"])),
        ).sql
        assert "COALESCE" not in sql

    def test_derived_metric_rebuilt_over_components(self) -> None:
        probe_model, result = _resolve(
            _with_metric("expression: '{[Avg Revenue per Customer]} / {[Revenue]}'")
        )
        assert result.valid, result.errors
        sql = _compile(
            probe_model,
            QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Probe"])),
        ).sql
        # The component is read from a column of its own, never a selected one,
        # taken from the plan before any wrapper ran.
        assert (
            '"reagg_1"."Avg Revenue per Customer" / NULLIF("reagg_components".'
            '"_reagg_component_1", 0)' in sql
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

    def test_having_valid(self) -> None:
        metric = {
            "type": "reaggregate",
            "measure": "R",
            "per": ["C"],
            "aggregation": "avg",
            "having": [{"field": "N", "op": ">", "value": 5}],
        }
        assert self._validate(metric) == []

    @pytest.mark.parametrize(
        "having",
        [[], [{"field": "N"}], [{"field": "N", "op": ">", "vale": 5}]],
        ids=["empty", "no op", "unknown key"],
    )
    def test_having_shape_refused(self, having: list[dict[str, Any]]) -> None:
        metric = {
            "type": "reaggregate",
            "measure": "R",
            "per": ["C"],
            "aggregation": "avg",
            "having": having,
        }
        assert self._validate(metric)

    def test_having_refused_on_derived(self) -> None:
        assert self._validate({"expression": "{[R]}", "having": [{"field": "R", "op": ">"}]})


class TestGraph:
    def test_rdf_triples(self, model: SemanticModel) -> None:
        g = export_obsl(model, "m")
        obsl = "https://ralforion.com/ns/obsl#"
        subjects = set(g.subjects(RDF.type, URIRef(obsl + "ReaggregateMetric")))
        assert len(subjects) == 3
        met = next(s for s in subjects if (s, URIRef(obsl + "per"), Literal("Order Date:day")) in g)
        assert (met, URIRef(obsl + "aggregation"), Literal("avg")) in g

    def test_over_a_reaggregate_metric_links_the_metric(self) -> None:
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Avg Daily Revenue\nper: [Customer]\naggregation: max"
            )
        )
        assert result.valid, result.errors
        g = export_obsl(probe_model, "m")
        obsl = "https://ralforion.com/ns/obsl#"
        probe, inner = (
            next(s for s in g.subjects(RDFS.label, Literal(n)))
            for n in ("Probe", "Avg Daily Revenue")
        )
        assert (probe, URIRef(obsl + "baseMetric"), inner) in g
        assert not set(g.objects(probe, URIRef(obsl + "baseMeasure")))

    def test_having_triples(self) -> None:
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Orders Count, op: '>', value: 5}, "
                "{field: Revenue, op: '<', value: 100}]"
            )
        )
        assert result.valid, result.errors
        g = export_obsl(probe_model, "m")
        probe = next(g.subjects(RDFS.label, Literal("Probe")))
        having = URIRef("https://ralforion.com/ns/obsl#reaggregateHaving")
        assert set(g.objects(probe, having)) == {
            Literal("Orders Count > 5"),
            Literal("Revenue < 100"),
        }

    def test_lineage_has_having_edges(self) -> None:
        probe_model, result = _resolve(
            _with_metric(
                "type: reaggregate\nmeasure: Revenue\nper: [Customer]\naggregation: avg\n"
                "having: [{field: Orders Count, op: '>', value: 5}]"
            )
        )
        assert result.valid, result.errors
        lineage = LineageBuilder(probe_model).metric("Probe")
        labels = {
            (next(n.name for n in lineage.nodes if n.id == e.source), e.label)
            for e in lineage.edges
        }
        assert ("Orders Count", "having") in labels

    def test_lineage_has_per_edges(self, model: SemanticModel) -> None:
        lineage = LineageBuilder(model).metric("Avg Daily Revenue")
        labels = {
            (next(n.name for n in lineage.nodes if n.id == e.source), e.label)
            for e in lineage.edges
        }
        assert ("Order Date", "per") in labels
        assert ("Revenue", None) in labels
