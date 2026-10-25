"""Calendar-aligned relative filters and period literals, on every engine.

The orders fall on the 15th of every month around the engine's own today, plus
one today, so each calendar filter has rows inside and outside it whatever day
the suite runs. The period-literal orders are in 2010, clear of those.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

ORDERS = "cdf_orders"

LITERAL_DAYS = [
    date(2009, 12, 31),
    date(2010, 1, 1),
    date(2010, 3, 31),
    date(2010, 4, 1),
    date(2010, 7, 4),
]

MODEL_YAML = """
version: "1.0"
name: calendar_date_filters_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: int, primaryKey: true}}
      Order Date: {{code: order_date, abstractType: date}}
dimensions:
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
"""


def _month_start(day: date, shift: int = 0) -> date:
    months = day.year * 12 + day.month - 1 + shift
    return date(months // 12, months % 12 + 1, 1)


def _quarter_start(day: date, shift: int = 0) -> date:
    return _month_start(date(day.year, 3 * ((day.month - 1) // 3) + 1, 1), 3 * shift)


def _today(target: VendorTarget) -> date:
    """The engine's current date, which the filters read, not this machine's."""
    dialect = DialectRegistry.get(target.dialect)
    rows = target.execute(f"SELECT {dialect.current_date_sql()} AS today")
    return date.fromisoformat(str(next(iter(rows[0].values())))[:10])


def _prepare(target: VendorTarget) -> tuple[SemanticModel, date, list[date]]:
    today = _today(target)
    relative_days = sorted(
        {_month_start(today, shift).replace(day=15) for shift in range(-24, 7)} | {today}
    )
    days = relative_days + LITERAL_DAYS
    _create(
        target,
        ORDERS,
        [("id", 2), ("order_date", 3)],
        [(i, day.isoformat()) for i, day in enumerate(days)],
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model, today, relative_days


def _days(target: VendorTarget, model: SemanticModel, where: dict) -> list[date]:
    query = QueryObject(**{"select": {"dimensions": ["Order Date"]}, "where": [where]})
    rows = _run(target, model, query)
    return sorted(date.fromisoformat(str(row["order date"])[:10]) for row in rows)


def _assert_all(target: VendorTarget) -> None:
    model, today, relative_days = _prepare(target)

    def within(start: date, end: date) -> list[date]:
        return [day for day in relative_days if start <= day < end]

    calendar_cases = [
        ({"unit": "quarter", "count": 1}, _quarter_start(today), _quarter_start(today, 1)),
        (
            {"unit": "month", "count": 1, "include_current": False},
            _month_start(today, -1),
            _month_start(today),
        ),
        (
            {"unit": "quarter", "count": 4, "include_current": False},
            _quarter_start(today, -4),
            _quarter_start(today),
        ),
        (
            {"unit": "month", "count": 2, "direction": "future", "include_current": False},
            _month_start(today, 1),
            _month_start(today, 3),
        ),
        (
            {"unit": "year", "count": 1, "toDate": True},
            date(today.year, 1, 1),
            today + timedelta(days=1),
        ),
    ]
    for value, start, end in calendar_cases:
        where = {"field": "Order Date", "op": "relative", "value": {**value, "calendar": True}}
        assert _days(target, model, where) == within(start, end), value

    literal_cases = [
        ("=", "2010-Q1", [date(2010, 1, 1), date(2010, 3, 31)]),
        ("<=", "2009-12", [date(2009, 12, 31)]),
        ("between", ["2010-01", "2010-03"], [date(2010, 1, 1), date(2010, 3, 31)]),
        ("in", ["2010-Q1", "2010-07-04"], [date(2010, 1, 1), date(2010, 3, 31), date(2010, 7, 4)]),
    ]
    for op, value, expected in literal_cases:
        assert _days(target, model, {"field": "Order Date", "op": op, "value": value}) == expected


def test_duckdb_calendar_date_filters(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_calendar_date_filters(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_calendar_date_filters(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_calendar_date_filters(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_calendar_date_filters(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_calendar_date_filters(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_calendar_date_filters(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_calendar_date_filters(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
