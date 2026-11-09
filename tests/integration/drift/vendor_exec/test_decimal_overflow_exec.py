"""A value past its decimal type is never answered with another number.

Revenue of 50,000,000,000 times a cost of 35,000,000,000 is 1.75e21, past the
``DECIMAL(18, 2)`` a metric is cast to by default. PostgreSQL, Snowflake,
Databricks, ClickHouse and DuckDB raise; MySQL, whose cast carries 38 digits,
and BigQuery return the value. Dremio returned 0.00, silently, and so did any
decimal cast past the type's integer digits there: its cast is tested now, and
the query fails. A value that fits, and the places a cast in a formula keeps,
are returned as before: 1e-11 cast to ``decimal(18, 12)`` and multiplied by 1.0
is 1e-11, and doubled from a filter context's measure 2e-11, which a cast
widened to 38 digits read as 0 on Dremio.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

ORDERS = "decimal_overflow_orders"

MODEL_YAML = """
version: "1.0"
name: decimal_overflow
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Amount: {{code: amount, abstractType: decimal}}
      Cost: {{code: cost, abstractType: decimal}}
      Fraction: {{code: fraction, abstractType: decimal}}
      Quantity: {{code: qty, abstractType: int}}
      Huge: {{code: huge, abstractType: float}}
      Edge: {{code: edge, abstractType: decimal}}
      Whole: {{code: whole, abstractType: decimal, sqlPrecision: 38, sqlScale: 0}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Cost Total: {{columns: [{{dataObject: Orders, column: Cost}}], aggregation: sum}}
  Fraction Total: {{columns: [{{dataObject: Orders, column: Fraction}}], aggregation: sum}}
  Rounded Up:
    expression: "cast(99.995, 'decimal(4,2)') + 0 * {{[Orders].[Amount]}}"
    aggregation: sum
  All Places:
    columns: [{{dataObject: Orders, column: Quantity}}]
    aggregation: sum
    dataType: "decimal(38, 38)"
  Huge Total:
    columns: [{{dataObject: Orders, column: Huge}}]
    aggregation: sum
    dataType: "decimal(18, 2)"
  Edge Total:
    columns: [{{dataObject: Orders, column: Edge}}]
    aggregation: sum
    dataType: "decimal(38, 2)"
  Widest:
    columns: [{{dataObject: Orders, column: Whole}}]
    aggregation: max
    dataType: "decimal(40, 2)"
  Fraction Unfiltered:
    columns: [{{dataObject: Orders, column: Fraction}}]
    aggregation: sum
    dataType: "decimal(18, 12)"
    filterContext: {{mode: FIXED}}
metrics:
  Revenue times Cost: {{expression: "{{[Revenue]}} * {{[Cost Total]}}"}}
  Margin: {{expression: "({{[Revenue]}} - {{[Cost Total]}}) / {{[Revenue]}}"}}
  Profit: {{expression: "{{[Revenue]}} * {{[Margin]}}"}}
  Tiny Cast:
    expression: "cast({{[Fraction Total]}}, 'decimal(18,12)') * 1.0"
    dataType: "decimal(18, 12)"
  Fraction Twice: {{expression: "{{[Fraction Unfiltered]}} * 2", dataType: "decimal(18, 12)"}}
  Constant Past: {{expression: "{{[Revenue]}} * 0 + cast(50 * 2, 'decimal(2,0)')"}}
  Text Cast: {{expression: "{{[Revenue]}} * 0 + cast('12.34', 'decimal(18,2)')"}}
  Null Cast: {{expression: "{{[Revenue]}} * 0 + cast(null, 'decimal(18,2)')"}}
  Wide Text Cast: {{expression: "{{[Revenue]}} * 0 + cast('1000000000', 'decimal(40,30)')"}}
"""

DECIMAL = {"bigquery": "NUMERIC", "clickhouse": "Nullable(Decimal(38, 2))"}
FRACTION = {"bigquery": "BIGNUMERIC", "clickhouse": "Nullable(Decimal(38, 14))"}
EDGE = {"bigquery": "BIGNUMERIC", "clickhouse": "Nullable(Decimal(38, 2))"}
WHOLE = {"bigquery": "BIGNUMERIC", "clickhouse": "Nullable(Decimal(38, 0))"}
# The largest value of DECIMAL(38, 2).
LARGEST = "999999999999999999999999999999999999.99"
# The engines whose cast carries 38 digits, or whose NUMERIC does: they return it.
WIDENED = {"mysql", "bigquery"}


def _prepare(target: VendorTarget) -> SemanticModel:
    kind = DECIMAL.get(target.dialect, "DECIMAL(38, 2)")
    fraction = FRACTION.get(target.dialect, "DECIMAL(38, 14)")
    edge = EDGE.get(target.dialect, "DECIMAL(38, 2)")
    whole = WHOLE.get(target.dialect, "DECIMAL(38, 0)")
    _create(
        target,
        ORDERS,
        [
            ("id", 0),
            ("amount", kind),
            ("cost", kind),
            ("fraction", fraction),
            ("qty", 2),
            ("huge", 1),
            ("edge", edge),
            ("whole", whole),
        ],
        [
            (
                "o1",
                "50000000000.00",
                "35000000000.00",
                "0.00000000001",
                1,
                1e40,
                LARGEST,
                "1" + "0" * 37,
            )
        ],
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _answer(target: VendorTarget, model: SemanticModel, measure: str) -> Any:
    rows = _run(target, model, QueryObject(select={"measures": [measure]}))
    return Decimal(str(rows[0][measure.lower()]))


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    # Within its type: returned exactly, as before, the places of a cast in a
    # formula kept through the arithmetic after it.
    assert _answer(target, model, "Profit") == Decimal("15000000000"), target.name
    if target.dialect != "bigquery":  # its NUMERIC keeps 9 places, whatever the cast
        assert _answer(target, model, "Tiny Cast") == Decimal("1E-11"), target.name
        # Read from the common table expression of its filter context.
        assert _answer(target, model, "Fraction Twice") == Decimal("2E-11"), target.name
    # Past it: an error, or the value itself, never another number.
    try:
        product = _answer(target, model, "Revenue times Cost")
    except Exception as exc:  # noqa: BLE001 - each driver raises its own type
        assert target.dialect not in WIDENED, target.name
        if target.dialect == "dremio":
            assert "OBSL: a value past DECIMAL(18, 2)" in str(exc), str(exc)
        return
    assert product == Decimal("1750000000000000000000"), (target.name, product)


def _assert_dremio_fails(target: VendorTarget) -> None:
    """Past its type in four ways a narrower test let through on Dremio: a
    literal past it once rounded (99.995 is 100.00), a type of 38 digits with
    no integer digit, a double past what the test's own decimal held, and a
    constant product, which Dremio's planner casts itself, to 100 where the
    executor reads 0, and a type past 38 digits, which Dremio casts at 38:
    ``decimal(40, 2)`` holds no 10^37 there. And within it: the largest value of a type of 38
    digits, which a test rounding a place fewer refused, text of a number and
    a NULL, which a test taking every literal it could not read as past the
    type refused."""
    model = _prepare(target)
    for measure in (
        "Rounded Up",
        "All Places",
        "Huge Total",
        "Constant Past",
        "Widest",
        "Wide Text Cast",
    ):
        with pytest.raises(Exception, match="OBSL: a value past DECIMAL"):
            _answer(target, model, measure)
    # The largest value of its type fits, as its own cast holds it, and so do
    # text of a number and a NULL, which cannot pass it.
    assert _answer(target, model, "Edge Total") == Decimal(LARGEST)
    assert _answer(target, model, "Text Cast") == Decimal("12.34")
    (row,) = _run(target, model, QueryObject(select={"measures": ["Null Cast"]}))
    assert row["null cast"] is None


def test_duckdb_decimal_overflow(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_decimal_overflow(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_decimal_overflow(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_decimal_overflow(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_decimal_overflow(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_decimal_overflow(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_decimal_overflow(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_decimal_overflow(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
    _assert_dremio_fails(vendor_dremio)
