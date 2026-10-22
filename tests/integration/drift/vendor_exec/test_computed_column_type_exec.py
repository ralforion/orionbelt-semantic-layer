"""A computed column declared ``abstractType: date`` returns a date.

Measured over a DATE column before the fix: ``date_trunc('month', d)`` came
back a timestamp on DuckDB and Databricks and a timestamptz on PostgreSQL;
``date_add('day', 1, d)`` a timestamp on DuckDB and PostgreSQL and a DATETIME
on BigQuery. MySQL, ClickHouse, Snowflake and Dremio returned dates. The
column is cast to its declared type now, as a grained dimension already was.
"""

from __future__ import annotations

import datetime
import re

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.parser import ReferenceResolver, TrackedLoader

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: computed_column_type_exec
dataObjects:
  Orders:
    code: orders
    columns:
      Id: {code: id, abstractType: string, primaryKey: true}
      Order Date: {code: order_date, abstractType: date}
      Order Month: {expression: "date_trunc('month', {Order Date})", abstractType: date}
      Next Day: {expression: "date_add('day', 1, {Order Date})", abstractType: date}
dimensions:
  Order Month: {dataObject: Orders, column: Order Month, resultType: date}
  Next Day: {dataObject: Orders, column: Next Day, resultType: date}
"""

# How each engine spells a DATE literal, where ``CAST('...' AS DATE)`` is not it.
DATE_LITERAL = {
    "bigquery": "DATE '2026-08-15'",
    "clickhouse": "toDate('2026-08-15')",
}

EXPECTED = {
    "Order Month": datetime.date(2026, 8, 1),
    "Next Day": datetime.date(2026, 8, 16),
}


def _assert_dates(target: VendorTarget) -> None:
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Order Month", "Next Day"], "measures": []}}
    )
    sql = CompilationPipeline().compile(query, model, target.dialect).sql
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    dia = DialectRegistry.get(target.dialect)
    literal = DATE_LITERAL.get(target.dialect, "CAST('2026-08-15' AS DATE)")
    source = (
        f"(SELECT {literal} AS {dia.quote_identifier('order_date')}) "
        f"{dia.quote_identifier('Orders')}"
    )
    row = target.execute(f"SELECT {match.group(1).strip()} FROM {source}")[0]
    got = {name: row[name] for name in EXPECTED}
    assert got == EXPECTED, f"{target.name}: {got}"
    assert all(type(v) is datetime.date for v in got.values()), f"{target.name}: {got}"


def test_duckdb_computed_date_column(vendor_duckdb: VendorTarget) -> None:
    _assert_dates(vendor_duckdb)


def test_postgres_computed_date_column(vendor_postgres: VendorTarget) -> None:
    _assert_dates(vendor_postgres)


def test_mysql_computed_date_column(vendor_mysql: VendorTarget) -> None:
    _assert_dates(vendor_mysql)


def test_clickhouse_computed_date_column(vendor_clickhouse: VendorTarget) -> None:
    _assert_dates(vendor_clickhouse)


def test_snowflake_computed_date_column(vendor_snowflake: VendorTarget) -> None:
    _assert_dates(vendor_snowflake)


def test_bigquery_computed_date_column(vendor_bigquery: VendorTarget) -> None:
    _assert_dates(vendor_bigquery)


def test_databricks_computed_date_column(vendor_databricks: VendorTarget) -> None:
    _assert_dates(vendor_databricks)


def test_dremio_computed_date_column(vendor_dremio: VendorTarget) -> None:
    _assert_dates(vendor_dremio)
