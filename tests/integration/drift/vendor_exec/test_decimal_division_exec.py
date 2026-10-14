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

MySQL's period-over-period ratio cast both sides to ``DECIMAL(38, 14)``, which
clipped 2e30 and 1e30 to the same maximum (a ratio of 1) and rounded 1e-20 to
0 (NULL). It divides natively now, as its formulas do: any widening costs
integer digits of a 65-digit decimal, and a grouped quotient past them is
clipped silently.

The north region has two sales, in January and February, of 50,000,000,000
and of 5,000 units, so its amount is 1e11 and its units 10,000.
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
    ("s1", "north", "2026-01-10", 50000000000.0, 5000, 1e30, 1, 300000, "0.00000000000000000001"),
    ("s2", "north", "2026-02-10", 50000000000.0, 5000, 2e30, 1, 300000, "0.00000000000000000002"),
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
      One: {{code: one, abstractType: int}}
      Many: {{code: many, abstractType: int}}
      Tiny: {{code: tiny, abstractType: float}}
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
  One Sum:
    columns: [{{dataObject: Sales, column: One}}]
    aggregation: sum
  Many Sum:
    columns: [{{dataObject: Sales, column: Many}}]
    aggregation: sum
  Tiny Sum:
    columns: [{{dataObject: Sales, column: Tiny}}]
    aggregation: sum
    dataType: decimal(38, 30)
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
  Small Share:
    expression: '{{[One Sum]}} / {{[Many Sum]}}'
    dataType: decimal(18, 10)
"""

METRICS = {
    # 1e11 / 3 at 2 places.
    "Amount over Three": Decimal("33333333333.33"),
    # A divisor of 16 places: 1e11 / 10,000.
    "Amount per Fine Unit": Decimal("10000000.00"),
    # A quotient over a quotient: (1e11 / 3) / (10,000 / 4).
    "Ratio of Ratios": Decimal("13333333.33"),
    # Integer sums, at 10 places: 2 / 600,000.
    "Small Share": Decimal("0.0000033333"),
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
    columns: list[tuple[str, int | str]] = [
        ("id", 0),
        ("region", 0),
        ("day", 3),
        ("amount", 1),
        ("units", 2),
        ("huge", 1),
        ("one", 2),
        ("many", 2),
        ("tiny", _decimal(target.dialect, 38, 30)),
    ]
    _create(target, SALES, columns, SALE_ROWS)
    model = _model(MODEL_YAML, target, sales=SALES)
    metrics = dict(METRICS)
    if target.dialect in ("mysql", "snowflake"):
        # Both divide at the dividend's places plus a few, and are left so
        # (dialects.md): MySQL keeps 4, since places cost it integer digits,
        # and Snowflake 6, since 12 would fail on a quotient from 1e26.
        del metrics["Small Share"]
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=list(metrics))),
    )
    got = {m: _number(rows[0][m.lower()]) for m in metrics}
    assert got == metrics, f"{target.name}: {got}"
    # A double far beyond any Decimal(38, 14): 3e30 / 3.
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=["Huge over Three"])),
    )
    huge = float(rows[0]["huge over three"])
    assert abs(huge / 1e30 - 1) < 1e-12, f"{target.name}: {huge}"

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


PERIOD_RATIOS = {"Amount": "Total Amount", "Huge": "Huge Sum", "Tiny": "Tiny Sum"}


CLIP = "decdiv_clip"

CLIP_MODEL_YAML = """
version: "1.0"
name: decimal_clip_vendor
dataObjects:
  Big:
    code: {clip}
    schema: '{schema}'
    columns:
      Region: {{code: region, abstractType: string}}
      A: {{code: a, abstractType: int}}
      Wide: {{code: wide, abstractType: int}}
dimensions:
  Region: {{dataObject: Big, column: Region, resultType: string}}
measures:
  Third Sum:
    expression: '{{[Big].[A]}} / 3'
    aggregation: sum
    dataType: decimal(38, 0)
  Fourfold Third Sum:
    expression: '{{[Big].[A]}} / 3 / 3 / 3 / 3'
    aggregation: sum
    dataType: decimal(38, 0)
  Wide Third Sum:
    expression: '{{[Big].[Wide]}} / 3'
    aggregation: sum
    dataType: decimal(65, 0)
"""


#: 1e37 / 3, 1e37 / 81 and 1e50 / 3, rounded to whole numbers.
CLIP_SUMS = {
    "Third Sum": Decimal("3" * 37),
    "Fourfold Third Sum": Decimal("123456790123456790123456790123456790"),
    "Wide Third Sum": Decimal("3" * 50),
}


def _assert_grouped_quotient_fits(target: VendorTarget) -> None:
    """MySQL stores a grouped ``SUM(a / 3)`` at the quotient's type, and
    clips a value beyond its integer digits to the maximum. A dividend widened
    to 30 places left 35 (1e37 / 3 came back 1e35), to 18 places 47 (1e50 /
    3 clipped), and each further division takes 4 more."""
    columns: list[tuple[str, int | str]] = [
        ("region", 0),
        ("a", "DECIMAL(38, 0)"),
        ("wide", "DECIMAL(65, 0)"),
    ]
    _create(target, CLIP, columns, [("north", "1" + "0" * 37, "1" + "0" * 50)])
    model = _model(CLIP_MODEL_YAML, target, clip=CLIP)
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region"], measures=list(CLIP_SUMS))),
    )
    got = {m: _number(rows[0][m.lower()]) for m in CLIP_SUMS}
    assert got == CLIP_SUMS, f"{target.name}: {got}"


def _ratio(value: object) -> float | None:
    return None if value is None else round(float(str(value)), 9)


def _assert_period_ratio(target: VendorTarget) -> None:
    """The period-over-period ratio builds its division as text. February over
    January: 5e10 over 5e10, 2e30 over 1e30, and 2e-20 over 1e-20."""
    yaml_extra = "".join(
        f"""
  {name} Ratio:
    type: period_over_period
    expression: '{{{{[{measure}]}}}}'
    periodOverPeriod:
      {{{{timeDimension: Month, grain: month, offset: -1, offsetGrain: month, comparison: ratio}}}}
"""
        for name, measure in PERIOD_RATIOS.items()
    )
    period_model = _model(MODEL_YAML + yaml_extra, target, sales=SALES)
    metrics = [f"{name} Ratio" for name in PERIOD_RATIOS]
    rows = _run(
        target,
        period_model,
        QueryObject(select=QuerySelect(dimensions=["Month"], measures=metrics)),
    )
    got = {
        str(r["month"])[:7]: tuple(_ratio(r[m.lower()]) for m in metrics)
        for r in rows
        if r[metrics[0].lower()] is not None
    }
    assert got == {"2026-02": (1.0, 2.0, 2.0)}, f"{target.name}: {got}"


def test_duckdb_decimal_division(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_decimal_division(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_decimal_division(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)
    _assert_grouped_quotient_fits(vendor_mysql)


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
