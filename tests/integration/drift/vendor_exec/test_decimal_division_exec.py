"""A decimal division keeps its value and its places on every engine.

ClickHouse divided at ``Decimal(38, 14)`` on both sides. Its decimal division
computes ``a * 10^s / b`` in the dividend's type without an overflow check, so
28 of the 38 digits went to scale: 100,000,000,000 / 3 came back
-694,903,358.76, silently. The cast also cut every scale above 14 (1e-38
became 0) and could not hold a double above 1e24 (DECIMAL_OVERFLOW).

The north region has two sales of 50,000,000,000 and of 5,000 units, so its
amount is 1e11 and its units 10,000.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _number, _run

pytestmark = pytest.mark.docker

SALES = "decdiv_sales"

SALE_ROWS = [
    ("s1", "north", "2026-01-10", 50000000000.0, 5000, 1e30),
    ("s2", "north", "2026-02-10", 50000000000.0, 5000, 1e30),
]

MODEL_YAML = """
version: "1.0"
name: decimal_division_vendor
dataObjects:
  Sales:
    code: {sales}
    schema: '{schema}'
    columns:
      Sale ID: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Day: {{code: day, abstractType: date}}
      Amount: {{code: amount, abstractType: float}}
      Units: {{code: units, abstractType: int}}
      Huge: {{code: huge, abstractType: float}}
dimensions:
  Region: {{dataObject: Sales, column: Region, resultType: string}}
  Month: {{dataObject: Sales, column: Day, resultType: date, timeGrain: month}}
measures:
  Total Amount:
    columns: [{{dataObject: Sales, column: Amount}}]
    aggregation: sum
    dataType: decimal(18, 2)
  Fine Units:
    columns: [{{dataObject: Sales, column: Units}}]
    aggregation: sum
    dataType: decimal(38, 16)
  Huge Sum:
    columns: [{{dataObject: Sales, column: Huge}}]
    aggregation: sum
    dataType: double
metrics:
  Amount over Three:
    expression: '{{[Total Amount]}} / 3'
    dataType: decimal(18, 2)
  Amount per Fine Unit:
    expression: '{{[Total Amount]}} / {{[Fine Units]}}'
    dataType: decimal(18, 2)
  Ratio of Ratios:
    expression: '({{[Total Amount]}} / 3) / ({{[Fine Units]}} / 4)'
    dataType: decimal(18, 2)
  Huge over Three:
    expression: '{{[Huge Sum]}} / 3'
    dataType: double
"""

METRICS = {
    # 1e11 / 3 at 2 places.
    "Amount over Three": Decimal("33333333333.33"),
    # A divisor of 16 places: 1e11 / 10,000.
    "Amount per Fine Unit": Decimal("10000000.00"),
    # A quotient over a quotient: (1e11 / 3) / (10,000 / 4).
    "Ratio of Ratios": Decimal("13333333.33"),
}

#: Each engine's ``DECIMAL(38, 38)``. MySQL allows at most 30 places and
#: Snowflake 37, so neither has one; BigQuery casts to no parameterized
#: type, and its BIGNUMERIC holds 38 places.
POINT_DECIMAL = {
    "duckdb": "DECIMAL(38, 38)",
    "postgres": "NUMERIC(38, 38)",
    "clickhouse": "Decimal(38, 38)",
    "bigquery": "BIGNUMERIC",
    "databricks": "DECIMAL(38, 38)",
    "dremio": "DECIMAL(38, 38)",
}

POINTS = "decdiv_points"

POINT_ROWS = [("north", "0.00000000000000000000000000000000000001")] * 2

POINT_MODEL_YAML = """
version: "1.0"
name: decimal_scale_vendor
dataObjects:
  Points:
    code: {points}
    schema: '{schema}'
    columns:
      Region: {{code: region, abstractType: string}}
      Point: {{code: point, abstractType: float}}
dimensions:
  Region: {{dataObject: Points, column: Region, resultType: string}}
measures:
  Point Sum:
    columns: [{{dataObject: Points, column: Point}}]
    aggregation: sum
    dataType: decimal(38, 38)
metrics:
  Point Half:
    expression: '{{[Point Sum]}} / 2'
    dataType: decimal(38, 38)
"""


def _model(yaml_template: str, target: VendorTarget, **tables: str) -> SemanticModel:
    yaml_text = yaml_template.format(schema=SCHEMAS.get(target.dialect, ""), **tables)
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _assert_all(target: VendorTarget) -> None:
    _create(
        target,
        SALES,
        [("id", 0), ("region", 0), ("day", 3), ("amount", 1), ("units", 2), ("huge", 1)],
        SALE_ROWS,
    )
    model = _model(MODEL_YAML, target, sales=SALES)
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=list(METRICS))),
    )
    got = {m: _number(rows[0][m.lower()]) for m in METRICS}
    assert got == METRICS, f"{target.name}: {got}"
    # A double far beyond any Decimal(38, 14): 2e30 / 3.
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=["Huge over Three"])),
    )
    huge = float(rows[0]["huge over three"])
    assert abs(huge / (2e30 / 3) - 1) < 1e-12, f"{target.name}: {huge}"

    # The period-over-period ratio builds its division as text: February's
    # 5e10 over January's 5e10.
    _assert_period_ratio(target)

    if target.dialect in POINT_DECIMAL:
        _assert_no_integer_digits(target)


def _assert_period_ratio(target: VendorTarget) -> None:
    yaml_extra = """
  Amount Ratio:
    type: period_over_period
    expression: '{{[Total Amount]}}'
    periodOverPeriod:
      {{timeDimension: Month, grain: month, offset: -1, offsetGrain: month, comparison: ratio}}
"""
    period_model = _model(MODEL_YAML + yaml_extra, target, sales=SALES)
    rows = _run(
        target,
        period_model,
        QueryObject(select=QuerySelect(dimensions=["Month"], measures=["Amount Ratio"])),
    )
    got = {str(r["month"])[:7]: _number(r["amount ratio"]) for r in rows}
    assert got.get("2026-02") == Decimal(1), f"{target.name}: {got}"


def _assert_no_integer_digits(target: VendorTarget) -> None:
    """A decimal with no integer digit keeps all 38 places: 2e-38 / 2."""
    _create(target, POINTS, [("region", 0), ("point", POINT_DECIMAL[target.dialect])], POINT_ROWS)
    model = _model(POINT_MODEL_YAML, target, points=POINTS)
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=["Point Half"])),
    )
    got = {r["region"]: _number(r["point half"]) for r in rows}
    assert got == {"north": Decimal("1e-38")}, f"{target.name}: {got}"


def test_duckdb_decimal_division(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_decimal_division(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_decimal_division(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_decimal_division(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_decimal_division(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_decimal_division(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_decimal_division(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_decimal_division(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
