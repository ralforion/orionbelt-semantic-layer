"""A cumulative metric orders a row without a date last, on every engine.

=========  ========  =============  ======
order      region    date           amount
=========  ========  =============  ======
``o1``     r1        2024-01-05     1
``o2``     r2        2024-03-09     2
``o3``     r1        *(none)*       4
``o4``     *(none)*  2024-03-01     8
=========  ========  =============  ======

The window left the NULL's place to the engine. MySQL, BigQuery and
Databricks sort NULLs first, so their running total read ``o3``'s 4 before
January and answered 5 there, where the other five answered 1.
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

ORDERS = "cumulative_null_orders"

MODEL_YAML = """
version: "1.0"
name: cumulative_null_order
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
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
  Revenue YTD:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
    grainToDate: year
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
            ("amount", 1),
        ],
        [
            ("o1", "r1", "2024-01-05", 1),
            ("o2", "r2", "2024-03-09", 2),
            ("o3", "r1", None, 4),
            ("o4", None, "2024-03-01", 8),
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


def _d(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _rows(target: VendorTarget, model: SemanticModel, dimensions: list[str]) -> list[tuple]:
    query = QueryObject(
        select={"dimensions": dimensions, "measures": ["Running Revenue", "Revenue YTD"]}
    )
    keys = [d.lower() for d in dimensions]
    result = [
        (
            *(_day(row[k]) if "month" in k else row[k] for k in keys),
            _d(row["running revenue"]),
            _d(row["revenue ytd"]),
        )
        for row in _run(target, model, query)
    ]
    return sorted(result, key=lambda r: tuple((v is None, v) for v in r[: len(keys)]))


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    # The undated row comes after every month, and its own year is no year.
    assert _rows(target, model, ["Order Month"]) == [
        (JAN, _d(1), _d(1)),
        (MAR, _d(11), _d(11)),
        (None, _d(15), _d(4)),
    ], target.name
    assert _rows(target, model, ["Region", "Order Month"]) == [
        ("r1", JAN, _d(1), _d(1)),
        ("r1", None, _d(5), _d(4)),
        ("r2", MAR, _d(2), _d(2)),
        (None, MAR, _d(8), _d(8)),
    ], target.name


def test_duckdb_cumulative_null_order(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_cumulative_null_order(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_cumulative_null_order(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_cumulative_null_order(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_cumulative_null_order(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_cumulative_null_order(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_cumulative_null_order(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_cumulative_null_order(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
