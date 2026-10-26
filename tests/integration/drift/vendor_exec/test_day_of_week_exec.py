"""``extract('day_of_week', x)`` is ISO, Monday 1 to Sunday 7, on every engine.

One date per weekday, 2026-08-10 (a Monday) to 2026-08-16 (a Sunday), read
through a computed column. Five engines are rewritten: BigQuery and Dremio
number Sunday 1, MySQL Monday 0, and Snowflake and Databricks spell the ISO
unit their own way. The catalog examples cover seven engines in
``test_function_exec.py``; this one adds Dremio and the whole week.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

DAYS = "dow_days"
MONDAY = date(2026, 8, 10)

MODEL_YAML = """
version: "1.0"
name: day_of_week_vendor
dataObjects:
  Days:
    code: {days}
    schema: '{schema}'
    columns:
      Day ID: {{code: id, abstractType: int, primaryKey: true}}
      Day: {{code: day, abstractType: date}}
      Weekday: {{expression: "extract('day_of_week', {{[Days].[Day]}})", abstractType: int}}
dimensions:
  Day: {{dataObject: Days, column: Day, resultType: date}}
  Weekday: {{dataObject: Days, column: Weekday, resultType: int}}
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    rows = [(i, (MONDAY + timedelta(days=i)).isoformat()) for i in range(7)]
    _create(target, DAYS, [("id", 2), ("day", 3)], rows)
    yaml_text = MODEL_YAML.format(days=DAYS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    query = QueryObject(**{"select": {"dimensions": ["Day", "Weekday"]}})
    rows = _run(target, model, query)
    weekdays = {str(row["day"])[:10]: int(row["weekday"]) for row in rows}
    assert weekdays == {(MONDAY + timedelta(days=i)).isoformat(): i + 1 for i in range(7)}


def test_duckdb_day_of_week(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_day_of_week(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_day_of_week(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_day_of_week(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_day_of_week(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_day_of_week(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_day_of_week(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_day_of_week(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
