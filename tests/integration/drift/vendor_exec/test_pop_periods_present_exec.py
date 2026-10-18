"""Period-over-period periods, empty priors and NULL groups, on every engine.

Only periods with rows are returned; a prior period without rows inside the
range reads empty (a count 0); a NULL group compares with its own prior
period, which needs a NULL-safe join on the dimensions.
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
from .test_reaggregate_exec import SCHEMAS, _create, _number, _run

pytestmark = pytest.mark.docker

ORDERS = "pop_present_orders"

# February has no orders; one order a month has no region.
ORDER_ROWS = [
    ("o1", "north", "2026-01-10", 10.0),
    ("o2", None, "2026-01-11", 5.0),
    ("o3", "north", "2026-03-10", 30.0),
    ("o4", "south", "2026-03-11", 7.0),
    ("o5", None, "2026-03-12", 6.0),
    ("o6", None, "2026-04-02", 4.0),
]

MODEL_YAML = """
version: "1.0"
name: pop_periods_present_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Order Date: {{code: order_date, abstractType: date}}
      Amount: {{code: amount, abstractType: float}}
dimensions:
  Region: {{dataObject: Orders, column: Region, resultType: string}}
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
metrics:
  Prior Revenue:
    type: period_over_period
    expression: '{{[Revenue]}}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Orders Count Change:
    type: period_over_period
    expression: '{{[Orders Count]}}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target, ORDERS, [("id", 0), ("region", 0), ("order_date", 3), ("amount", 1)], ORDER_ROWS
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _month(value: Any) -> str:
    return str(value)[:7]


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Order Date", "Region"],
                    "measures": ["Prior Revenue", "Orders Count Change"],
                }
            }
        ),
    )
    got = sorted(
        (
            (
                _month(r["order date"]),
                r["region"],
                _number(r["prior revenue"]),
                _number(r["orders count change"]),
            )
            for r in rows
        ),
        key=str,
    )
    assert got == [
        ("2026-01", "north", None, None),
        ("2026-01", None, None, None),
        ("2026-03", "north", None, Decimal(1)),
        ("2026-03", "south", None, Decimal(1)),
        ("2026-03", None, None, Decimal(1)),
        ("2026-04", None, Decimal(6), Decimal(0)),
    ]

    # From March on: the priors come from the look-back, February empty there.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {"dimensions": ["Order Date"], "measures": ["Orders Count Change"]},
                "where": [{"field": "Order Date", "op": ">=", "value": "2026-03-01"}],
            }
        ),
    )
    assert sorted((_month(r["order date"]), _number(r["orders count change"])) for r in rows) == [
        ("2026-03", Decimal(3)),
        ("2026-04", Decimal(-2)),
    ]


def test_duckdb_pop_periods_present(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_pop_periods_present(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_pop_periods_present(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_pop_periods_present(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_pop_periods_present(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_pop_periods_present(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_pop_periods_present(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_pop_periods_present(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
