"""A ``where`` filter on a grained dimension compares its period, on every engine.

======  ==========  ======
order   date        amount
======  ==========  ======
``o1``  2025-01-05  10
``o2``  2025-01-20  1
``o3``  2025-02-03  2
======  ==========  ======
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
from .test_fill_exec import TIMESTAMP
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

ORDERS = "grained_filter_orders"

MODEL_YAML = """
version: "1.0"
name: grained_filter_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Date: {{code: order_date, abstractType: date}}
      Order Time: {{code: order_time, abstractType: timestamp}}
      Amount: {{code: amount, abstractType: float}}
dimensions:
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
  Order Week: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: week}}
  Order Time Month:
    dataObject: Orders
    column: Order Time
    resultType: timestamp
    timeGrain: month
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
"""

JAN, FEB = date(2025, 1, 1), date(2025, 2, 1)


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target,
        ORDERS,
        [("id", 0), ("order_date", 3), ("order_time", TIMESTAMP[target.dialect]), ("amount", 1)],
        [
            ("o1", "2025-01-05", "2025-01-05 10:00:00", 10),
            ("o2", "2025-01-20", "2025-01-20 10:00:00", 1),
            ("o3", "2025-02-03", "2025-02-03 10:00:00", 2),
        ],
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _revenue(
    target: VendorTarget, model: SemanticModel, dimension: str, field: str, op: str, value: Any
) -> dict[date, Decimal]:
    query = QueryObject(
        select={"dimensions": [dimension], "measures": ["Revenue"]},
        where=[{"field": field, "op": op, "value": value}],
    )
    return {
        _day(row[dimension.lower()]): Decimal(str(row["revenue"])).quantize(Decimal("0.01"))
        for row in _run(target, model, query)
    }


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    month = "Order Month"
    cases: list[tuple[str, str, str, Any, dict[date, Decimal]]] = [
        (month, month, "equals", "2025-01-01", {JAN: Decimal("11.00")}),
        (month, month, "equals", "2025-01-20", {}),
        (month, month, "gte", "2025-01-15", {FEB: Decimal("2.00")}),
        ("Order Week", "Order Week", "equals", "2025-01-20", {date(2025, 1, 20): Decimal("1.00")}),
        (
            "Order Time Month",
            "Order Time Month",
            "gt",
            "2025-01-01 00:00:00",
            {FEB: Decimal("2.00")},
        ),
    ]
    for dimension, field, op, value, expected in cases:
        got = _revenue(target, model, dimension, field, op, value)
        assert got == expected, f"{target.name} {field} {op} {value}"


def test_duckdb_grained_filter(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_grained_filter(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_grained_filter(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_grained_filter(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_grained_filter(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_grained_filter(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_grained_filter(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_grained_filter(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
