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
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Cost Total: {{columns: [{{dataObject: Orders, column: Cost}}], aggregation: sum}}
  Fraction Total: {{columns: [{{dataObject: Orders, column: Fraction}}], aggregation: sum}}
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
"""

DECIMAL = {"bigquery": "NUMERIC", "clickhouse": "Nullable(Decimal(38, 2))"}
FRACTION = {"bigquery": "BIGNUMERIC", "clickhouse": "Nullable(Decimal(38, 14))"}
# The engines whose cast carries 38 digits, or whose NUMERIC does: they return it.
WIDENED = {"mysql", "bigquery"}


def _prepare(target: VendorTarget) -> SemanticModel:
    kind = DECIMAL.get(target.dialect, "DECIMAL(38, 2)")
    fraction = FRACTION.get(target.dialect, "DECIMAL(38, 14)")
    _create(
        target,
        ORDERS,
        [("id", 0), ("amount", kind), ("cost", kind), ("fraction", fraction)],
        [("o1", "50000000000.00", "35000000000.00", "0.00000000001")],
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
