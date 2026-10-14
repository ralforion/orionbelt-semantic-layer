"""A decimal division keeps its value and its places on every engine.

ClickHouse divided at ``Decimal(38, 14)`` on both sides. Its decimal division
computes ``a * 10^s / b`` in the dividend's type without an overflow check, so
28 of the 38 digits went to scale: 100,000,000,000 / 3 came back
-694,903,358.76, silently. The cast also cut every scale above 14 (1e-38
became 0) and could not hold a double above 1e24 (DECIMAL_OVERFLOW). Keeping
the operands' scale instead wrapped a quotient of 38 places divided again, and
refused a divisor with more places than its dividend; ClickHouse now divides
in Float64. The ``DECIMAL(38, 38)`` boundary is in
``test_integer_division_exec.py``.

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
from .test_integer_division_exec import POINT_DECIMAL
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


SCALES = "decdiv_scales"

#: Halves of 0.9, 0.1, 10 and 2, so each sums to the whole over two rows.
SCALE_ROWS = [("north", "0.45", "0.05", "5.00", "1.0")] * 2


#: Each engine's ``DECIMAL(p, s)``, so that the sums reach the division as
#: decimals; BigQuery casts to no parameterized type.
def _decimal(dialect: str, precision: int, scale: int) -> str:
    if dialect == "bigquery":
        return "BIGNUMERIC"
    name = {"postgres": "NUMERIC", "clickhouse": "Decimal", "snowflake": "NUMBER"}
    return f"{name.get(dialect, 'DECIMAL')}({precision}, {scale})"


SCALE_MODEL_YAML = """
version: "1.0"
name: decimal_scale_division_vendor
dataObjects:
  Scales:
    code: {scales}
    schema: '{schema}'
    columns:
      Region: {{code: region, abstractType: string}}
      A: {{code: a, abstractType: float}}
      B: {{code: b, abstractType: float}}
      Ten: {{code: ten, abstractType: float}}
      Two: {{code: two, abstractType: float}}
dimensions:
  Region: {{dataObject: Scales, column: Region, resultType: string}}
measures:
  Fine A:
    columns: [{{dataObject: Scales, column: A}}]
    aggregation: sum
    dataType: decimal(38, 38)
  Fine B:
    columns: [{{dataObject: Scales, column: B}}]
    aggregation: sum
    dataType: decimal(38, 38)
  Coarse Ten:
    columns: [{{dataObject: Scales, column: Ten}}]
    aggregation: sum
    dataType: decimal(18, 2)
  Fine Two:
    columns: [{{dataObject: Scales, column: Two}}]
    aggregation: sum
    dataType: decimal(38, 25)
metrics:
  Twice Divided:
    expression: '({{[Fine A]}} / {{[Fine B]}}) / {{[Fine B]}}'
    dataType: decimal(18, 2)
  Over a Finer Divisor:
    expression: '{{[Coarse Ten]}} / {{[Fine Two]}}'
    dataType: decimal(18, 2)
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
    _assert_scales(target)


def _assert_scales(target: VendorTarget) -> None:
    """A divisor with more places than its dividend: 10 at 2 places over 2 at
    25. And where the engine has 38 places, a quotient of them divided again:
    (0.9 / 0.1) / 0.1, which kept at 38 places outgrew 76 digits and wrapped.
    """
    # Where the engine has no 38 places (MySQL, Snowflake), 30.
    fine = POINT_DECIMAL.get(target.dialect, _decimal(target.dialect, 38, 30))
    columns: list[tuple[str, int | str]] = [
        ("region", 0),
        ("a", fine),
        ("b", fine),
        ("ten", _decimal(target.dialect, 18, 2)),
        ("two", _decimal(target.dialect, 38, 25)),
    ]
    _create(target, SCALES, columns, SCALE_ROWS)
    model = _model(SCALE_MODEL_YAML, target, scales=SCALES)
    metrics = {"Over a Finer Divisor": Decimal("5.00")}
    if target.dialect in POINT_DECIMAL:
        metrics["Twice Divided"] = Decimal("90.00")
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=list(metrics))),
    )
    got = {m: _number(rows[0][m.lower()]) for m in metrics}
    assert got == metrics, f"{target.name}: {got}"


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
