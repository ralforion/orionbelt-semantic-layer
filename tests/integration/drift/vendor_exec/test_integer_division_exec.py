"""A metric dividing integers is a true division on every engine.

Postgres divides a bigint by an integer as integers, and ``COUNT(*)`` is a
bigint; Dremio does so for every integer type, and its ``SUM`` of an integer
stays one. A count, a sum or a ratio of them over an integer came back 0
there, where the other engines answer 0.5. Each region has two sales of one
unit, so every metric below is 0.5.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _number, _run

pytestmark = pytest.mark.docker

SALES = "intdiv_sales"

ROWS = [
    ("s1", "north", 1, 0.000000000001),
    ("s2", "north", 1, 0.000000000001),
    ("s3", "south", 1, 0.000000000001),
    ("s4", "south", 1, 0.000000000001),
]

MODEL_YAML = """
version: "1.0"
name: integer_division_vendor
dataObjects:
  Sales:
    code: {sales}
    schema: '{schema}'
    columns:
      Sale ID: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Quantity: {{code: quantity, abstractType: int}}
      Tiny: {{code: tiny, abstractType: float}}
dimensions:
  Region: {{dataObject: Sales, column: Region, resultType: string}}
measures:
  Sales Rows:
    columns: [{{dataObject: Sales, column: Sale ID}}]
    aggregation: count
  Sold Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
  Typed Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
    resultType: int
  Tiny Sum:
    columns: [{{dataObject: Sales, column: Tiny}}]
    aggregation: sum
metrics:
  Rows over Four:
    expression: '{{[Sales Rows]}} / 4'
  Quantity over Four:
    expression: '{{[Sold Quantity]}} / 4'
  Typed over Four:
    expression: '{{[Typed Quantity]}} / 4'
  Rows over Double Quantity:
    expression: '{{[Sales Rows]}} / ({{[Sold Quantity]}} * 2)'
  Tiny over Four:
    expression: '{{[Tiny Sum]}} / 4'
    dataType: double
"""

METRICS = ["Rows over Four", "Quantity over Four", "Typed over Four", "Rows over Double Quantity"]


def _assert_all(target: VendorTarget) -> None:
    _create(target, SALES, [("id", 0), ("region", 0), ("quantity", 2), ("tiny", 1)], ROWS)
    yaml_text = MODEL_YAML.format(sales=SALES, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=[*METRICS, "Tiny over Four"])
        ),
    )
    got = {r["region"]: tuple(_number(r[m.lower()]) for m in METRICS) for r in rows}
    half = (Decimal("0.5"),) * len(METRICS)
    assert got == {"north": half, "south": half}, f"{target.name}: {got}"
    # A double dividend keeps its precision: 2e-12 / 4, not a fixed decimal
    # that rounds it to 0.
    tiny = {r["region"]: float(r["tiny over four"]) for r in rows}
    assert all(abs(v - 5e-13) < 1e-18 for v in tiny.values()), f"{target.name}: {tiny}"


def test_duckdb_integer_division(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_integer_division(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_integer_division(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_integer_division(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_integer_division(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_integer_division(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_integer_division(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_integer_division(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
