"""A ClickHouse time grain keeps a date or an instant before 1970.

Measured before the fix: ``toStartOfMonth`` and the rest of its family answer
``Date`` / ``DateTime``, which start in 1970, so every month of 1959 read
1970-01-01; ``toDate`` read 1959-12-31 as 2139, ``toStartOfHour`` a 1959
timestamp as 2096, and the Sunday week's ``toStartOfWeek`` 1970-01-01 again.
``dateTrunc`` keeps the wide types but rounds a pre-1970 fraction of a second
up, so the last instant of 1959 fell in 1960. The value is that last instant
for that reason.
"""

from __future__ import annotations

import datetime
import re

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.parser import ReferenceResolver, TrackedLoader

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: clickhouse_pre_1970_grain_exec
settings:
  weekStart: {week_start}
dataObjects:
  Orders:
    code: orders
    columns:
      Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Date: {{code: order_date, abstractType: date}}
      Placed At: {{code: placed_at, abstractType: timestamp}}
      Shipped At: {{code: shipped_at, abstractType: timestamp}}
      Placed Day Hour:
        expression: "date_trunc('hour', date_trunc('day', {{Placed At}}))"
        abstractType: timestamp
      Shipped Day Hour:
        expression: "date_trunc('hour', date_trunc('day', {{Shipped At}}))"
        abstractType: timestamp
      Placed Day Plus:
        expression: "date_add('hour', 1, date_trunc('day', {{Placed At}}))"
        abstractType: timestamp
      Order Day Start: {{expression: "date_trunc('day', {{Order Date}})", abstractType: timestamp}}
      Placed Month: {{expression: "date_trunc('month', {{Placed At}})", abstractType: date}}
      Placed Week: {{expression: "date_trunc('week', {{Placed At}})", abstractType: date}}
dimensions:
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
  Placed At: {{dataObject: Orders, column: Placed At, resultType: timestamp}}
  Shipped At: {{dataObject: Orders, column: Shipped At, resultType: timestamp}}
  Placed Day Hour: {{dataObject: Orders, column: Placed Day Hour, resultType: timestamp}}
  Shipped Day Hour: {{dataObject: Orders, column: Shipped Day Hour, resultType: timestamp}}
  Placed Day Plus: {{dataObject: Orders, column: Placed Day Plus, resultType: timestamp}}
  Order Day Start: {{dataObject: Orders, column: Order Day Start, resultType: timestamp}}
  Placed Month: {{dataObject: Orders, column: Placed Month, resultType: date}}
  Placed Week: {{dataObject: Orders, column: Placed Week, resultType: date}}
"""

SOURCE = (
    "(SELECT toDate32('1959-12-31') AS order_date, "
    "toDateTime64('1959-12-31 23:59:59.987', 3, 'UTC') AS placed_at, "
    "toDateTime('2026-08-15 10:30:45', 'UTC') AS shipped_at) \"Orders\""
)

DATE_GRAINS = {
    "Order Date:year": datetime.date(1959, 1, 1),
    "Order Date:quarter": datetime.date(1959, 10, 1),
    "Order Date:month": datetime.date(1959, 12, 1),
    "Order Date:week": datetime.date(1959, 12, 28),
    "Order Date:day": datetime.date(1959, 12, 31),
    "Placed Month": datetime.date(1959, 12, 1),
    "Placed Week": datetime.date(1959, 12, 28),
}

# A timestamp dimension stays a timestamp at a date grain.
TIME_GRAINS = {
    "Placed At:year": datetime.datetime(1959, 1, 1),
    "Placed At:month": datetime.datetime(1959, 12, 1),
    "Placed At:day": datetime.datetime(1959, 12, 31),
    "Placed At:hour": datetime.datetime(1959, 12, 31, 23, 0),
    "Placed At:minute": datetime.datetime(1959, 12, 31, 23, 59),
    "Placed At:second": datetime.datetime(1959, 12, 31, 23, 59, 59),
    # A plain DateTime, which toStartOfSecond refused outright.
    "Shipped At:second": datetime.datetime(2026, 8, 15, 10, 30, 45),
    # date_trunc('day', ...) in an expression stays a timestamp at midnight, so
    # an hour of it, or an hour added to it, compiles.
    "Placed Day Hour": datetime.datetime(1959, 12, 31),
    "Shipped Day Hour": datetime.datetime(2026, 8, 15),
    "Placed Day Plus": datetime.datetime(1959, 12, 31, 1),
    "Order Day Start": datetime.datetime(1959, 12, 31),
}


def _value(target: VendorTarget, dimension: str, week_start: str = "monday") -> object:
    """*dimension* alone, over the one row of :data:`SOURCE`."""
    raw, sm = TrackedLoader().load_string(MODEL_YAML.format(week_start=week_start))
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    query = QueryObject.model_validate({"select": {"dimensions": [dimension], "measures": []}})
    sql = CompilationPipeline().compile(query, model, target.dialect).sql
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    [row] = target.execute(f"SELECT {match.group(1).strip()} FROM {SOURCE}")
    [value] = row.values()
    return value


def test_date_grains_before_1970(vendor_clickhouse: VendorTarget) -> None:
    got = {name: _value(vendor_clickhouse, name) for name in DATE_GRAINS}
    assert got == DATE_GRAINS


def test_time_grains_before_1970(vendor_clickhouse: VendorTarget) -> None:
    got = {name: _value(vendor_clickhouse, name) for name in TIME_GRAINS}
    assert {name: v.replace(tzinfo=None) for name, v in got.items()} == TIME_GRAINS  # type: ignore[attr-defined]


def test_sunday_week_before_1970(vendor_clickhouse: VendorTarget) -> None:
    sunday = datetime.date(1959, 12, 27)
    assert _value(vendor_clickhouse, "Order Date:week", "sunday") == sunday
    assert _value(vendor_clickhouse, "Placed Week", "sunday") == sunday
