"""A window metric orders a NULL last, up or down, on every engine.

=========  ========  =============  ======
order      region    date           amount
=========  ========  =============  ======
``o1``     r1        2024-01-05     1
``o2``     r2        2024-03-09     2
``o3``     r1        *(none)*       4
``o4``     r3        2024-03-01     *(none)*
=========  ========  =============  ======

The window left the NULL's place to the engine. Ranked down by revenue,
PostgreSQL, Snowflake and Dremio ranked r3, which has none, first; ranked up,
MySQL, BigQuery and Databricks did. And they made the undated row's month the
one before January, so January's prior revenue read its 4. A running total
and a framed window already ordered it last.
"""

from __future__ import annotations

from datetime import date, datetime
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

ORDERS = "window_null_orders"

MODEL_YAML = """
version: "1.0"
name: window_null_order
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Order Date: {{code: order_date, abstractType: date}}
      Amount: {{code: amount, abstractType: float}}
dimensions:
  Region: {{dataObject: Orders, column: Region, resultType: string}}
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
metrics:
  Rank Down: {{type: window, windowFunction: dense_rank, measure: Revenue}}
  Rank Up: {{type: window, windowFunction: dense_rank, measure: Revenue, orderDirection: asc}}
  Prior Revenue:
    type: window
    windowFunction: lag
    measure: Revenue
    offset: 1
    timeDimension: Order Month
"""

JAN, MAR = date(2024, 1, 1), date(2024, 3, 1)


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target,
        ORDERS,
        [
            ("id", 0),
            ("region", 0),
            ("order_date", "Nullable(Date)" if target.dialect == "clickhouse" else 3),
            ("amount", "Nullable(Float64)" if target.dialect == "clickhouse" else 1),
        ],
        [
            ("o1", "r1", "2024-01-05", 1),
            ("o2", "r2", "2024-03-09", 2),
            ("o3", "r1", None, 4),
            ("o4", "r3", "2024-03-01", None),
        ],
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _day(value: Any) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)

    ranks = _run(
        target,
        model,
        QueryObject(select={"dimensions": ["Region"], "measures": ["Rank Down", "Rank Up"]}),
    )
    # r1 has 5, r2 2 and r3 none, which ranks last either way.
    assert sorted((r["region"], int(r["rank down"]), int(r["rank up"])) for r in ranks) == [
        ("r1", 1, 2),
        ("r2", 2, 1),
        ("r3", 3, 3),
    ], target.name

    prior = _run(
        target,
        model,
        QueryObject(select={"dimensions": ["Order Month"], "measures": ["Prior Revenue"]}),
    )
    # The undated row's month comes after March: January has no prior month.
    found = {_day(r["order month"]): _number(r["prior revenue"]) for r in prior}
    assert found == {JAN: None, MAR: _number(1), None: _number(2)}, target.name


def test_duckdb_window_null_order(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_window_null_order(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_window_null_order(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_window_null_order(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_window_null_order(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_window_null_order(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_window_null_order(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_window_null_order(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
