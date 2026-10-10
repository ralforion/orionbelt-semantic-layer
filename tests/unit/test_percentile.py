"""``percentile_cont`` and ``percentile_disc`` measures: model, compile, dialects.

The answers themselves are asserted on all eight engines in
``tests/integration/drift/vendor_exec/test_percentile_exec.py``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from orionbelt.ast.nodes import ColumnRef, FunctionCall
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.base import Dialect
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import Measure, SemanticModel
from orionbelt.obsl.exporter import export_obsl
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

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
      Country: {code: COUNTRY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
dimensions:
  Country: {dataObject: Orders, column: Country, resultType: string}
measures:
"""


def _resolve(measures: str) -> tuple[SemanticModel, list[str]]:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML + measures)
    model, result = ReferenceResolver().resolve(raw, source_map)
    return model, [e.message for e in result.errors]


def _sql(measures: str, measure: str, dialect: str = "duckdb") -> str:
    model, errors = _resolve(measures)
    assert not errors, errors
    query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=[measure]))
    return CompilationPipeline().compile(query, model, dialect).sql


def _measure(**fields: Any) -> Measure:
    base: dict[str, Any] = {
        "name": "P",
        "columns": [{"dataObject": "Orders", "column": "Amount"}],
        "aggregation": "percentile_cont",
        "percentile": 0.9,
    }
    return Measure.model_validate({**base, **fields})


P90 = """\
  P90:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: percentile_disc
    percentile: 0.9
"""


class TestMeasureValidation:
    @pytest.mark.parametrize(
        "aggregation", ["percentile_cont", "percentile_disc", "PERCENTILE_CONT"]
    )
    def test_valid(self, aggregation: str) -> None:
        assert _measure(aggregation=aggregation).percentile == 0.9

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"percentile": None}, "requires 'percentile'"),
            ({"aggregation": "sum"}, "only valid with aggregation"),
            ({"percentile": 0}, "between 0 and 1"),
            ({"percentile": 1}, "between 0 and 1"),
            ({"percentile": 1.5}, "between 0 and 1"),
            ({"percentile": -0.1}, "between 0 and 1"),
            ({"percentile": True}, "between 0 and 1"),
            ({"percentile": 0.1234567891}, "at most 9 decimal places"),
            ({"columns": []}, "exactly 1 column"),
            (
                {
                    "columns": [
                        {"dataObject": "Orders", "column": "Amount"},
                        {"dataObject": "Orders", "column": "Amount"},
                    ]
                },
                "exactly 1 column",
            ),
            ({"distinct": True}, "does not take 'distinct'"),
        ],
    )
    def test_refused(self, fields: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            _measure(**fields)

    def test_nine_places_allowed(self) -> None:
        assert _measure(percentile=0.123456789).percentile == 0.123456789

    def test_expression_allowed(self) -> None:
        measure = _measure(columns=[], expression="{[Orders].[Amount]} * 2")
        assert measure.expression is not None

    def test_resolver_reports_the_model_error(self) -> None:
        _model, errors = _resolve(
            "  P:\n    columns: [{dataObject: Orders, column: Amount}]\n"
            "    aggregation: percentile_cont\n"
        )
        assert any("requires 'percentile'" in e for e in errors), errors


class TestCompile:
    def test_ordered_set_aggregate(self) -> None:
        assert 'PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY "Orders"."AMOUNT")' in _sql(P90, "P90")

    def test_fraction_in_plain_notation(self) -> None:
        sql = _sql(P90.replace("0.9", "0.00001"), "P90")
        assert "PERCENTILE_DISC(0.00001) WITHIN GROUP" in sql

    def test_filter_wraps_the_value_not_the_fraction(self) -> None:
        filtered = P90 + (
            "    filters:\n"
            "      - column: {dataObject: Orders, column: Country}\n"
            "        operator: equals\n"
            "        values: [{dataType: string, valueString: DE}]\n"
        )
        sql = _sql(filtered, "P90")
        assert "PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY CASE WHEN" in sql

    def test_default_value_wraps_the_aggregate(self) -> None:
        sql = _sql(P90 + "    defaultValue: 0\n", "P90")
        assert "COALESCE(PERCENTILE_DISC(0.9) WITHIN GROUP" in sql

    def test_untyped_result(self) -> None:
        """As a median: a default decimal would round the interpolated value."""
        sql = _sql(P90.replace("disc", "cont"), "P90")
        assert "AS DECIMAL" not in sql

    def test_total_refused(self) -> None:
        with pytest.raises(Exception, match="does not support total"):
            _sql(P90 + "    total: true\n", "P90")


def _call(name: str, fraction: str = "0.3") -> FunctionCall:
    return FunctionCall(name=name, args=[ColumnRef(name="x")], fraction=Decimal(fraction))


class TestDialects:
    @pytest.mark.parametrize(
        ("fraction", "ratio"),
        [("0.9", (9, 10)), ("0.25", (25, 100)), ("0.00001", (1, 100000)), ("0.5", (5, 10))],
    )
    def test_fraction_as_a_ratio(self, fraction: str, ratio: tuple[int, int]) -> None:
        assert Dialect.percentile_ratio(Decimal(fraction)) == ratio

    def test_a_call_without_a_fraction_refused(self) -> None:
        call = FunctionCall(name="PERCENTILE_CONT", args=[ColumnRef(name="x")])
        with pytest.raises(ValueError, match="needs a fraction"):
            DialectRegistry.get("postgres").compile_expr(call)

    @pytest.mark.parametrize(
        ("dialect", "cont", "disc"),
        [
            (
                "postgres",
                'PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY "x")',
                'PERCENTILE_DISC(0.3) WITHIN GROUP (ORDER BY "x")',
            ),
            (
                "duckdb",
                'PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY CAST("x" AS DOUBLE))',
                'PERCENTILE_DISC(0.3) WITHIN GROUP (ORDER BY "x")',
            ),
            (
                "dremio",
                'PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY CAST("x" AS DOUBLE))',
                'PERCENTILE_DISC(0.3) WITHIN GROUP (ORDER BY "x")',
            ),
            (
                "snowflake",
                'PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY CAST("x" AS DOUBLE))',
                'PERCENTILE_DISC(0.3) WITHIN GROUP (ORDER BY "x")',
            ),
            (
                "databricks",
                "PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY `x`)",
                "get(array_sort(collect_list(`x`)), CAST((3 * count(`x`) + 9) DIV 10 AS INT) - 1)",
            ),
            (
                "clickhouse",
                'quantileExactInclusive(0.3)(toFloat64("x"))',
                'arraySort(groupArray("x"))[intDiv(3 * count("x") + 9, 10)]',
            ),
            (
                "bigquery",
                "* (10 - MOD(3 * (COUNT(`x`) - 1), 10)) / 10 END",
                "[SAFE_OFFSET(DIV(3 * COUNT(`x`) + 9, 10) - 1)]",
            ),
            (
                "mysql",
                "* (CAST(3 * (COUNT(`x`) - 1) MOD 10 AS DOUBLE) / 10))",
                "(3 * COUNT(`x`) + 9) DIV 10",
            ),
        ],
    )
    def test_rendering(self, dialect: str, cont: str, disc: str) -> None:
        compiler = DialectRegistry.get(dialect)
        assert cont in compiler.compile_expr(_call("PERCENTILE_CONT"))
        assert disc in compiler.compile_expr(_call("PERCENTILE_DISC"))

    @pytest.mark.parametrize("dialect", ["bigquery", "clickhouse", "databricks", "mysql"])
    def test_positions_in_integers(self, dialect: str) -> None:
        """No double times a count: ``0.3 * 10`` is 3.0000000000000004."""
        compiler = DialectRegistry.get(dialect)
        for name in ("PERCENTILE_CONT", "PERCENTILE_DISC"):
            sql = compiler.compile_expr(_call(name))
            assert "0.3 *" not in sql, sql

    @pytest.mark.parametrize("dialect", ["clickhouse", "mysql"])
    def test_a_whole_operand(self, dialect: str) -> None:
        """A derived metric's ``* 2`` applies to the whole percentile."""
        sql = DialectRegistry.get(dialect).compile_expr(_call("PERCENTILE_CONT"))
        assert sql.startswith(("if(", "(")) and sql.endswith(")")

    def test_bigquery_steps_from_the_end_the_sign_rounds_away_from(self) -> None:
        """Up from ``lower`` when that is above zero, else down from ``upper``;
        whole parts towards zero, so nothing overflows, and one rounding."""
        sql = DialectRegistry.get("bigquery").compile_expr(_call("PERCENTILE_CONT"))
        assert sql.startswith("CASE WHEN MOD(") and sql.endswith(" END")
        assert " > 0 THEN " in sql and " ELSE " in sql
        assert "TRUNC(" in sql and "FLOOR(" not in sql
        assert " * BIGNUMERIC '1')" in sql
        # No difference of the two values is taken.
        assert "SAFE_SUBTRACT" not in sql and "SAFE_MULTIPLY" not in sql


class TestSchema:
    def _errors(self, measure: dict[str, Any]) -> list[str]:
        doc = {
            "version": 1.0,
            "dataObjects": {
                "O": {
                    "code": "O",
                    "database": "d",
                    "schema": "s",
                    "columns": {"C": {"code": "C", "abstractType": "float"}},
                }
            },
            "measures": {"M": {"columns": [{"dataObject": "O", "column": "C"}], **measure}},
        }
        return [e.message for e in jsonschema.Draft7Validator(_SCHEMA).iter_errors(doc)]

    def test_valid(self) -> None:
        assert self._errors({"aggregation": "percentile_disc", "percentile": 0.9}) == []

    @pytest.mark.parametrize(
        "measure",
        [
            {"aggregation": "percentile_cont"},
            {"aggregation": "sum", "percentile": 0.9},
            {"aggregation": "percentile_cont", "percentile": 1},
            {"aggregation": "percentile_cont", "percentile": 0},
        ],
    )
    def test_refused(self, measure: dict[str, Any]) -> None:
        assert self._errors(measure)


def test_exported_to_the_graph() -> None:
    model, errors = _resolve(P90)
    assert not errors, errors
    turtle = export_obsl(model, "m").serialize(format="turtle")
    assert "obsl:percentile 0.9" in turtle or 'obsl:percentile "0.9"^^xsd:decimal' in turtle
