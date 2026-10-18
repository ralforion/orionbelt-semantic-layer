"""A period-over-period query keeps the rows without a period, on every engine.

The customers without orders are added to the PoP source as a branch of its
own, rooted at the customers, with the orders joined on ``1 = 0`` (typed
NULL columns); an order without a date and those customers are the NULL
month, with no prior period.
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

REGIONS = "pop_pwc_regions"
CUSTOMERS = "pop_pwc_customers"
CHANNELS = "pop_pwc_channels"
ORDERS = "pop_pwc_orders"

REGION_ROWS = [("r1", 100.0), ("r2", 200.0)]
# c3 and c4 have no orders; o4 has no date. c4, with no credit, is alone in r2.
CUSTOMER_ROWS = [
    ("c1", "r1", "gold", 10.0),
    ("c2", "r1", "silver", 20.0),
    ("c3", "r1", "gold", 40.0),
    ("c4", "r2", "gold", 0.0),
]
CHANNEL_ROWS = [("h1",)]
ORDER_ROWS = [
    ("o1", "c1", "h1", "2026-01-05", 100.0),
    ("o2", "c1", "h1", "2026-02-05", 50.0),
    ("o3", "c2", "h1", "2026-02-07", 70.0),
    ("o4", "c2", "h1", None, 9.0),
]

MODEL_YAML = """
version: "1.0"
name: pop_parents_without_children_vendor
dataObjects:
  Regions:
    code: {regions}
    schema: '{schema}'
    columns:
      Region ID: {{code: id, abstractType: string, primaryKey: true}}
      Population: {{code: population, abstractType: float}}
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer ID: {{code: id, abstractType: string, primaryKey: true}}
      Customer Region ID: {{code: region_id, abstractType: string}}
      Tier: {{code: tier, abstractType: string}}
      Credit: {{code: credit, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
  Channels:
    code: {channels}
    schema: '{schema}'
    columns:
      Channel ID: {{code: id, abstractType: string, primaryKey: true}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer ID: {{code: customer_id, abstractType: string}}
      Order Channel ID: {{code: channel_id, abstractType: string}}
      Order Date: {{code: order_date, abstractType: date}}
      Amount: {{code: amount, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Channels
        columnsFrom: [Order Channel ID]
        columnsTo: [Channel ID]
dimensions:
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
  Tier: {{dataObject: Customers, column: Tier, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
  Total Population:
    columns: [{{dataObject: Regions, column: Population}}]
    aggregation: sum
    resultType: float
  Order Lines:
    expression: 'COALESCE({{[Orders].[Amount]}}, 0) + 1'
    aggregation: sum
    resultType: float
  Population With Orders:
    columns: [{{dataObject: Regions, column: Population}}]
    aggregation: sum
    resultType: float
    filters:
      - column: {{dataObject: Orders, column: Order ID}}
        operator: set
metrics:
  Prior Revenue:
    type: period_over_period
    expression: '{{[Revenue]}}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, REGIONS, [("id", 0), ("population", 1)], REGION_ROWS)
    _create(
        target,
        CUSTOMERS,
        [("id", 0), ("region_id", 0), ("tier", 0), ("credit", 1)],
        CUSTOMER_ROWS,
    )
    _create(target, CHANNELS, [("id", 0)], CHANNEL_ROWS)
    # One order has no date: ClickHouse's Date is not Nullable on its own.
    date_type: int | str = "Nullable(Date)" if target.dialect == "clickhouse" else 3
    _create(
        target,
        ORDERS,
        [
            ("id", 0),
            ("customer_id", 0),
            ("channel_id", 0),
            ("order_date", date_type),
            ("amount", 1),
        ],
        ORDER_ROWS,
    )
    yaml_text = MODEL_YAML.format(
        regions=REGIONS,
        customers=CUSTOMERS,
        channels=CHANNELS,
        orders=ORDERS,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _month(value: Any) -> str | None:
    return None if value is None else str(value)[:7]


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Order Month"],
                    "measures": ["Revenue", "Total Credit", "Prior Revenue"],
                }
            }
        ),
    )
    got = sorted(
        (
            (
                _month(r["order month"]),
                _number(r["revenue"]),
                _number(r["total credit"]),
                _number(r["prior revenue"]),
            )
            for r in rows
        ),
        key=str,
    )
    assert got == [
        ("2026-01", Decimal(100), Decimal(10), None),
        ("2026-02", Decimal(120), Decimal(30), Decimal(100)),
        (None, Decimal(9), Decimal(60), None),
    ]

    # Carol passes ``Tier = gold OR Tier notset``; Bob's order without a date
    # does not.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Order Month"],
                    "measures": ["Total Credit", "Prior Revenue"],
                },
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Tier", "op": "=", "value": "gold"},
                            {"field": "Tier", "op": "notset"},
                        ],
                    }
                ],
            }
        ),
    )
    got = sorted(
        (
            (_month(r["order month"]), _number(r["total credit"]), _number(r["prior revenue"]))
            for r in rows
        ),
        key=str,
    )
    assert got == [
        ("2026-01", Decimal(10), None),
        ("2026-02", Decimal(10), Decimal(100)),
        (None, Decimal(40), None),
    ]

    # Region r1 is reached through Bob's order without a date and through
    # Carol: one region, its population once. Carol has no order, so
    # ``COALESCE(amount, 0) + 1`` reads nothing for her, and the region's
    # population filtered on ``Order ID set`` is read from Bob's order.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Order Month"],
                    "measures": [
                        "Total Population",
                        "Total Credit",
                        "Order Lines",
                        "Population With Orders",
                        "Prior Revenue",
                    ],
                }
            }
        ),
    )
    got = sorted(
        (
            (
                _month(r["order month"]),
                _number(r["total population"]),
                _number(r["total credit"]),
                _number(r["order lines"]),
                _number(r["population with orders"]),
            )
            for r in rows
        ),
        key=str,
    )
    assert got == [
        ("2026-01", Decimal(100), Decimal(10), Decimal(101), Decimal(100)),
        ("2026-02", Decimal(100), Decimal(30), Decimal(122), Decimal(100)),
        (None, Decimal(300), Decimal(60), Decimal(10), Decimal(100)),
    ]

    # r2 is reached only through c4, who has no orders: its population is the
    # regions' own row without children, under no tier, not c4's gold too.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Order Month", "Tier"],
                    "measures": [
                        "Total Population",
                        "Total Credit",
                        "Order Lines",
                        "Prior Revenue",
                    ],
                }
            }
        ),
    )
    got = sorted(
        (
            (
                _month(r["order month"]),
                r["tier"],
                _number(r["total population"]),
                _number(r["total credit"]),
                _number(r["order lines"]),
            )
            for r in rows
        ),
        key=str,
    )
    assert got == [
        ("2026-01", "gold", Decimal(100), Decimal(10), Decimal(101)),
        ("2026-02", "gold", Decimal(100), Decimal(10), Decimal(51)),
        ("2026-02", "silver", Decimal(100), Decimal(20), Decimal(71)),
        (None, "gold", None, Decimal(40), None),
        (None, "silver", Decimal(100), Decimal(20), Decimal(10)),
        (None, None, Decimal(200), None, None),
    ]


def test_duckdb_pop_parents_without_children(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_pop_parents_without_children(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_pop_parents_without_children(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_pop_parents_without_children(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_pop_parents_without_children(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_pop_parents_without_children(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_pop_parents_without_children(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_pop_parents_without_children(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
