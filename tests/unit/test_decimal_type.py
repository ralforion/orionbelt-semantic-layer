"""``decimal``: an exact number, compiled as ``float`` compiles one.

The type adds a word to the model, not a behaviour to the compiler: a measure
or a multi-fact query over ``decimal`` columns compiles to the SQL the same
model declaring ``float`` does, on every dialect. Only where an abstract type
becomes a SQL type of its own does ``decimal`` render differently, as the
engine's exact decimal rather than its float.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import duckdb
import jsonschema
import pytest

from orionbelt.ast.nodes import BinaryOp, Cast, ColumnRef, Literal
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.window_wrap import partition_keys
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import DataType, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]

MODEL_YAML = """
version: "1.0"
name: decimal_type
dataObjects:
  Customers:
    code: customers
    columns:
      Customer Id: {{code: id, abstractType: string, primaryKey: true}}
      Credit: {{code: credit, abstractType: {t}, numClass: additive}}
  Orders:
    code: orders
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: string}}
      Amount: {{code: amount, abstractType: {t}, numClass: additive}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Order Customer],
          columnsTo: [Customer Id]}}
  Refunds:
    code: refunds
    columns:
      Refund Id: {{code: id, abstractType: string, primaryKey: true}}
      Refund Customer: {{code: customer_id, abstractType: string}}
      Refunded: {{code: refunded, abstractType: {t}}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Refund Customer],
          columnsTo: [Customer Id]}}
dimensions:
  Customer: {{dataObject: Customers, column: Customer Id, resultType: string}}
  Credit Line: {{dataObject: Customers, column: Credit, resultType: {t}}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: {t}
  Average Order:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: avg
    resultType: {t}
  Large Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: {t}
    filters:
      - column: {{dataObject: Orders, column: Amount}}
        operator: gt
        values: [{{dataType: {t}, valueFloat: 1.5}}]
  Refunds:
    columns: [{{dataObject: Refunds, column: Refunded}}]
    aggregation: sum
    resultType: {t}
"""


def _model(type_name: str) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML.format(t=type_name))
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _sql(type_name: str, query: QueryObject, dialect: str) -> str:
    return CompilationPipeline().compile(query, _model(type_name), dialect).sql


def test_a_decimal_model_loads_and_matches_the_schema() -> None:
    """``abstractType``, ``resultType`` and a filter value's ``dataType`` all
    take it, and a ``decimal`` column takes ``numClass`` as a ``float`` one does."""
    model = _model("decimal")
    assert model.data_objects["Orders"].columns["Amount"].abstract_type is DataType.DECIMAL
    assert model.measures["Revenue"].result_type is DataType.DECIMAL
    schema = json.loads(Path("schema/obml-schema.json").read_text())
    raw, _ = TrackedLoader().load_string(MODEL_YAML.format(t="decimal"))
    payload = json.loads(json.dumps(raw))
    payload["version"] = 1.0  # the schema types it as a number, YAML here as text
    jsonschema.validate(payload, schema)


QUERIES = {
    "star": QueryObject(
        select={
            "dimensions": ["Customer", "Credit Line"],
            "measures": ["Revenue", "Average Order", "Large Revenue"],
        }
    ),
    "multi-fact": QueryObject(
        select={"dimensions": ["Customer"], "measures": ["Revenue", "Refunds"]}
    ),
}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("shape", sorted(QUERIES))
def test_decimal_compiles_as_float_does(shape: str, dialect: str) -> None:
    query = QUERIES[shape]
    assert _sql("decimal", query, dialect) == _sql("float", query, dialect)


@pytest.mark.parametrize(
    ("dialect", "rendered"),
    [
        ("bigquery", "BIGNUMERIC"),
        ("clickhouse", "Decimal(38, 10)"),
        ("databricks", "DECIMAL(38, 10)"),
        ("dremio", "DECIMAL(38, 10)"),
        ("duckdb", "DECIMAL(38, 10)"),
        ("mysql", "DECIMAL(65, 10)"),
        ("postgres", "DECIMAL(38, 10)"),
        ("snowflake", "DECIMAL(38, 10)"),
    ],
)
def test_an_abstract_decimal_cast_is_exact(dialect: str, rendered: str) -> None:
    """A typed NULL pad or a nested field's read, cast to ``decimal``."""
    sql = DialectRegistry.get(dialect).compile_expr(Cast(expr=Literal.null(), type_name="decimal"))
    assert rendered in sql


def test_the_cast_keeps_a_cent_exact_on_duckdb() -> None:
    """As ``float`` the sum is 0.30000000000000004; as ``decimal``, 0.3."""
    dialect = DialectRegistry.get("duckdb")

    def as_decimal(value: str) -> Cast:
        return Cast(expr=Literal.string(value), type_name="decimal")

    total = dialect.compile_expr(BinaryOp(as_decimal("0.1"), "+", as_decimal("0.2")))
    assert duckdb.sql(f"SELECT {total}").fetchone() == (Decimal("0.3"),)


def test_bigquery_partitions_by_a_decimal_dimension_as_by_a_float() -> None:
    """A column declared ``decimal`` may still be FLOAT64 in BigQuery, which
    refuses to partition by it, so the key is cast to STRING as a float's is."""
    keys = partition_keys(["Credit Line"], _model("decimal"), DialectRegistry.get("bigquery"))
    assert keys == [Cast(expr=ColumnRef(name="Credit Line"), type_name="STRING")]


PRECISE_YAML = """
version: "1.0"
name: decimal_precise
dataObjects:
  Ledger:
    code: ledger
    columns:
      Entry Id: {{code: id, abstractType: string, primaryKey: true}}
      Amount: {{code: amount, abstractType: decimal, numClass: additive}}
      Rate: {{code: rate, abstractType: decimal, sqlPrecision: 38, sqlScale: 12}}
      Fee: {{code: fee, abstractType: decimal}}
dimensions:
  Rate: {{dataObject: Ledger, column: Rate, resultType: decimal}}
  Fee: {{dataObject: Ledger, column: Fee, resultType: decimal}}
measures:
  Matching Amount:
    columns: [{{dataObject: Ledger, column: Amount}}]
    aggregation: sum
    resultType: decimal
    filters:
      - column: {{dataObject: Ledger, column: Amount}}
        operator: equals
        values: [{{dataType: decimal, valueFloat: {value}}}]
"""


def _precise_model(
    value: str = "9007199254740993", *, as_json: bool = False
) -> tuple[SemanticModel, list[str]]:
    """The ledger model, loaded from YAML or, with *as_json*, as a JSON body
    (``model_json``), whose numbers arrive as bare doubles."""
    raw, source_map = TrackedLoader().load_string(PRECISE_YAML.format(value=value))
    if as_json:
        raw, source_map = json.loads(json.dumps(raw)), None
    model, result = ReferenceResolver().resolve(raw, source_map)
    errors = result.errors + SemanticValidator().validate(model)
    return model, [error.code for error in errors]


def _matching_amount(model: SemanticModel, amounts: list[str]) -> Decimal | None:
    """Run the filtered measure on DuckDB over ledger rows with *amounts*."""
    query = QueryObject(select={"measures": ["Matching Amount"]})
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    db = duckdb.connect()
    db.execute("CREATE TABLE ledger (id VARCHAR, amount DECIMAL(38, 18))")
    for i, amount in enumerate(amounts):
        db.execute("INSERT INTO ledger VALUES (?, CAST(? AS DECIMAL(38, 18)))", [str(i), amount])
    # An OSI round trip qualifies the table with the default schema.
    db.execute('CREATE SCHEMA "PUBLIC"')
    db.execute('CREATE VIEW "PUBLIC".ledger AS SELECT * FROM main.ledger')
    row = db.execute(sql).fetchone()
    return None if row is None else row[0]


class TestADecimalFilterValueIsExact:
    """A ``decimal`` filter value compares the number written, not its double.

    9007199254740993 is 2**53 + 1, the first integer a double cannot hold: as a
    float it is 9007199254740992, and the filter selected the other row.
    ``1.0000000000000001`` is 1.0 as a double, which YAML has already produced
    by the time the model sees it, so the loader keeps the written digits.
    """

    def test_the_literal_is_the_number_written(self) -> None:
        model, errors = _precise_model()
        assert errors == []
        query = QueryObject(select={"measures": ["Matching Amount"]})
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert "= 9007199254740993" in sql
        assert "9007199254740992" not in sql

    def test_the_filter_selects_the_right_row(self) -> None:
        model, _ = _precise_model()
        total = _matching_amount(model, ["9007199254740992", "9007199254740993"])
        assert total == Decimal("9007199254740993")

    def test_a_fraction_past_a_doubles_precision_selects_the_right_row(self) -> None:
        """Two rows hold the value written and one holds 1: comparing 1.0 sums 1."""
        model, errors = _precise_model("1.0000000000000001")
        assert errors == []
        rows = ["1", "1.0000000000000001", "1.0000000000000001"]
        assert _matching_amount(model, rows) == Decimal("2")

    @pytest.mark.parametrize(
        ("written", "exact"),
        [
            ("0.1", "0.1"),
            ("12345678901234567.89", "12345678901234567.89"),
            ("1_000.5", "1000.5"),
            ("-1.5e-3", "-0.0015"),
        ],
    )
    def test_the_written_digits_are_kept(self, written: str, exact: str) -> None:
        model, errors = _precise_model(written)
        assert errors == []
        value = model.measures["Matching Amount"].filters[0].values[0]  # type: ignore[union-attr]
        assert value.decimal_value == Decimal(exact)

    def test_a_fraction_without_its_digits_is_refused(self) -> None:
        """A JSON body hands over 1.0, and nothing says 1.0000000000000001 was
        meant, so the value is refused rather than compared as 1.0."""
        _, errors = _precise_model("1.0000000000000001", as_json=True)
        assert "INEXACT_DECIMAL_VALUE" in errors

    def test_a_whole_double_without_its_digits_is_refused_too(self) -> None:
        """1.0 may itself be 1.0000000000000001 rounded; a JSON integer is not."""
        _, errors = _precise_model("1.0", as_json=True)
        assert "INEXACT_DECIMAL_VALUE" in errors
        model, errors = _precise_model("9007199254740993", as_json=True)
        assert errors == []
        value = model.measures["Matching Amount"].filters[0].values[0]  # type: ignore[union-attr]
        assert value.decimal_value == Decimal("9007199254740993")

    def test_the_compiler_refuses_it_too(self) -> None:
        """A model built without the validator still does not compare NULL."""
        model, _ = _precise_model("0.5", as_json=True)
        query = QueryObject(select={"measures": ["Matching Amount"]})
        with pytest.raises(ResolutionError, match="not an exact number"):
            CompilationPipeline().compile(query, model, "duckdb")

    def test_a_float_filter_value_is_unchanged(self) -> None:
        """Only ``decimal`` reads the exact value; ``float`` stays a double."""
        from orionbelt.models.semantic import FilterValue

        value = FilterValue(data_type=DataType.FLOAT, value_float=9007199254740993)
        assert value.decimal_value is None
        assert not value.is_inexact_decimal


class TestADecimalColumnKeepsItsDeclaredWidth:
    """A ``decimal`` column with ``sqlPrecision``/``sqlScale`` is advertised at
    that width: 0.123456789012 does not fit ``decimal128(38, 10)``."""

    def test_the_result_schema_takes_the_declared_width(self) -> None:
        import pyarrow as pa

        from orionbelt.service.result_schema import declared_result_schema

        model, _ = _precise_model()
        query = QueryObject(select={"dimensions": ["Rate", "Fee"]})
        schema = declared_result_schema(query, model)
        assert schema.field("Rate").type == pa.decimal128(38, 12)
        assert schema.field("Fee").type == pa.decimal128(38, 10)

    def test_the_declared_types_agree(self) -> None:
        import pyarrow as pa

        from orionbelt.service.result_schema import declared_arrow_types, raw_field_arrow_type

        model, _ = _precise_model()
        assert declared_arrow_types(model)["Rate"] == pa.decimal128(38, 12)
        assert raw_field_arrow_type("Ledger.Rate", model) == pa.decimal128(38, 12)

    def test_a_scale_twelve_value_streams_as_advertised(self) -> None:
        """The value Arrow refused to rescale into (38, 10) fits the schema."""
        import pyarrow as pa

        from orionbelt.service.result_schema import declared_result_schema

        model, _ = _precise_model()
        query = QueryObject(select={"dimensions": ["Rate"]})
        schema = declared_result_schema(query, model)
        table = pa.table({"Rate": pa.array([Decimal("0.123456789012")], pa.decimal128(38, 12))})
        assert table.cast(schema).column("Rate")[0].as_py() == Decimal("0.123456789012")


class TestDecimalDigitsAsText:
    """``valueString`` holds a ``decimal`` value's digits, which no JSON or YAML
    writer rounds - the form exact decimals take wherever a float would not
    survive (a JSON body, OSI metadata)."""

    def test_text_digits_select_the_right_rows(self) -> None:
        yaml_text = PRECISE_YAML.format(value="0").replace(
            "valueFloat: 0", "valueString: '1.0000000000000001'"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.errors + SemanticValidator().validate(model) == []
        rows = ["1", "1.0000000000000001", "1.0000000000000001"]
        assert _matching_amount(model, rows) == Decimal("2")

    def test_text_that_is_not_a_number_is_refused(self) -> None:
        yaml_text = PRECISE_YAML.format(value="0").replace(
            "valueFloat: 0", "valueString: 'one point five'"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, _ = ReferenceResolver().resolve(raw, source_map)
        codes = [e.code for e in SemanticValidator().validate(model)]
        assert "INEXACT_DECIMAL_VALUE" in codes

    def test_pinning_writes_the_known_digits_as_text(self) -> None:
        from orionbelt.parser.loader import pin_decimal_digits

        raw, _ = TrackedLoader().load_string(PRECISE_YAML.format(value="1.0000000000000001"))
        pinned = pin_decimal_digits(raw)
        value = pinned["measures"]["Matching Amount"]["filters"][0]["values"][0]
        assert value == {"dataType": "decimal", "valueString": "1.0000000000000001"}

    def test_pinning_leaves_a_float_without_digits_for_the_reader(self) -> None:
        from orionbelt.parser.loader import pin_decimal_digits

        value = {"dataType": "decimal", "valueFloat": 1.0}
        assert pin_decimal_digits(value) == value

    def test_the_converters_yaml_parse_keeps_the_digits(self) -> None:
        from orionbelt.parser.loader import load_yaml_keeping_decimals

        data = load_yaml_keeping_decimals(PRECISE_YAML.format(value="1.0000000000000001"))
        value = data["measures"]["Matching Amount"]["filters"][0]["values"][0]
        assert value["valueString"] == "1.0000000000000001"


class TestDecimalDigitsSurviveOSI:
    """Export to Apache Ossie and back keeps a ``decimal`` filter value exact.

    The converter wrote the double into both the exported expression and the
    round-trip metadata, so 1.0000000000000001 came back 1.0 and the measure
    summed one row instead of two. This is the session export/import path:
    the stored raw is pinned, converted, and the result loaded back as YAML.
    """

    def _osi(self) -> dict[str, object]:
        from osi_orionbelt import OBMLtoOSI

        from orionbelt.parser.loader import pin_decimal_digits

        raw, _ = TrackedLoader().load_string(PRECISE_YAML.format(value="1.0000000000000001"))
        converter = OBMLtoOSI(pin_decimal_digits(raw))
        osi = converter.convert()
        assert not any("decimal filter value" in w for w in converter.warnings)
        return osi

    def test_the_exported_expression_keeps_the_digits(self) -> None:
        assert "= 1.0000000000000001" in json.dumps(self._osi())

    def test_the_round_trip_computes_what_the_model_did(self) -> None:
        import yaml
        from osi_orionbelt import OSItoOBML

        obml = OSItoOBML(self._osi()).convert()
        text = yaml.dump(obml, default_flow_style=False, allow_unicode=True, sort_keys=False)
        raw, source_map = TrackedLoader().load_string(text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.errors + SemanticValidator().validate(model) == []
        rows = ["1", "1.0000000000000001", "1.0000000000000001"]
        assert _matching_amount(model, rows) == Decimal("2")
