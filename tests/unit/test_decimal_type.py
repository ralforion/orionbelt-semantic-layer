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
from orionbelt.compiler.window_wrap import partition_keys
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import DataType, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

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
