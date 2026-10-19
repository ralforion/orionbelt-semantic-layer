"""``fill: true`` on every engine: each period of the time dimension.

=========  ========  =============  ======
order      region    date           amount
=========  ========  =============  ======
``o1``     r1        2024-01-05     1
``o2``     r2        2024-03-09     2
``o3``     r1        *(none)*       4
``o4``     *(none)*  2024-03-01     8
=========  ========  =============  ======

February has no orders and is added; ``o3`` has no date and stays a row. The
same months are read off a ``DATE`` and a ``TIMESTAMP`` column, since the
date spine has its own type on some engines.
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

ORDERS = "fill_orders"

#: A timestamp, as each engine spells it in a CAST.
TIMESTAMP = {
    "duckdb": "TIMESTAMP",
    "postgres": "TIMESTAMP",
    "mysql": "DATETIME",
    "clickhouse": "Nullable(DateTime)",
    "snowflake": "TIMESTAMP_NTZ",
    "bigquery": "DATETIME",
    "databricks": "TIMESTAMP_NTZ",
    "dremio": "TIMESTAMP",
}

MODEL_YAML = """
version: "1.0"
name: fill_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Order Date: {{code: order_date, abstractType: date}}
      Order Time: {{code: order_time, abstractType: timestamp}}
      Amount: {{code: amount, abstractType: float}}
dimensions:
  Region: {{dataObject: Orders, column: Region, resultType: string}}
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
  Order Time Month:
    dataObject: Orders
    column: Order Time
    resultType: timestamp
    timeGrain: month
  Order Week: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: week}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Revenue Or Zero:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
    defaultValue: 0
"""

JAN, FEB, MAR, APR = date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1), date(2024, 4, 1)
DEC = date(2023, 12, 1)


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target,
        ORDERS,
        [
            ("id", 0),
            ("region", 0),
            ("order_date", "Nullable(Date)" if target.dialect == "clickhouse" else 3),
            ("order_time", TIMESTAMP[target.dialect]),
            ("amount", 1),
        ],
        [
            ("o1", "r1", "2024-01-05", "2024-01-05 10:00:00", 1),
            ("o2", "r2", "2024-03-09", "2024-03-09 10:00:00", 2),
            ("o3", "r1", None, None, 4),
            ("o4", None, "2024-03-01", "2024-03-01 00:00:00", 8),
        ],
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _day(value: Any) -> date | None:
    """A period as a date, from a driver's date, datetime or string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _filled(
    target: VendorTarget, model: SemanticModel, dimensions: list[str], where: list[dict]
) -> list[tuple]:
    query = QueryObject(
        select={
            "dimensions": dimensions,
            "measures": ["Revenue", "Revenue Or Zero", "Orders Count"],
        },
        where=where,
        fill=True,
    )
    rows = _run(target, model, query)
    keys = [d.lower() for d in dimensions]
    result = [
        (
            *(_day(row[k]) if "month" in k else row[k] for k in keys),
            _number(row["revenue"]),
            _number(row["revenue or zero"]),
            int(row["orders count"]),
        )
        for row in rows
    ]
    # NULLs last, so the order does not depend on the engine's NULL ordering.
    return sorted(result, key=lambda r: tuple((v is None, v) for v in r[: len(keys)]))


def _d(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(f"{value:.2f}")


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    for month in ("Order Month", "Order Time Month"):
        assert _filled(target, model, [month], []) == [
            (JAN, _d(1), _d(1), 1),
            (FEB, None, _d(0), 0),
            (MAR, _d(10), _d(10), 2),
            (None, _d(4), _d(4), 1),
        ], f"{target.name} {month}"
    bounds = [
        {"field": "Order Month", "op": "gte", "value": "2023-12-15"},
        {"field": "Order Month", "op": "lt", "value": "2024-05-01"},
    ]
    r1 = {"field": "Region", "op": "equals", "value": "r1"}
    for where in (
        bounds,
        [{"logic": "and", "filters": bounds}],
        [{"logic": "and", "filters": [bounds[0], r1, bounds[1]]}],
    ):
        assert [row[0] for row in _filled(target, model, ["Order Month"], where)] == [
            DEC,
            JAN,
            FEB,
            MAR,
            APR,
        ], target.name
    for value, last in (("2024-04-01 12:00:00", APR), ("2024-04-01 00:00:00", MAR)):
        before = [{"field": "Order Time Month", "op": "lt", "value": value}]
        periods = [row[0] for row in _filled(target, model, ["Order Time Month"], before)]
        assert periods[-1] == last, f"{target.name} < {value}"
    weeks = [{"field": "Order Week", "op": "between", "value": ["2024-02-20", "2024-03-10"]}]
    assert [(r[0], r[3]) for r in _filled(target, model, ["Order Week"], weeks)] == [
        (date(2024, 2, 19), 0),
        (date(2024, 2, 26), 1),
        (date(2024, 3, 4), 1),
    ], target.name
    by_region = _filled(
        target,
        model,
        ["Region", "Order Month"],
        [{"field": "Order Month", "op": "between", "value": ["2024-01-01", "2024-03-31"]}],
    )
    assert [(r[0], r[1], r[2]) for r in by_region] == [
        ("r1", JAN, _d(1)),
        ("r1", FEB, None),
        ("r1", MAR, None),
        ("r2", JAN, None),
        ("r2", FEB, None),
        ("r2", MAR, _d(2)),
        (None, JAN, None),
        (None, FEB, None),
        (None, MAR, _d(8)),
    ], target.name


def test_duckdb_fill(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_fill(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_fill(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_fill(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_fill(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_fill(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_fill(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_fill(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
