"""A computed column declaring a date or time is that type, on every engine.

``date_trunc`` and ``date_add`` over a DATE return a timestamp on DuckDB and
PostgreSQL (a timestamptz there), a DATETIME from BigQuery's ``date_add`` and a
TIMESTAMP from Databricks' ``date_trunc``. A computed column declared
``abstractType: date`` came back as a timestamp on those engines and as a date
on the others. It is cast to its declared type now, the way a grained
dimension already was.
"""

from __future__ import annotations

import datetime
import re

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
dataObjects:
  Orders:
    code: orders
    columns:
      Id: {code: id, abstractType: string, primaryKey: true}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
      Order Month: {expression: "date_trunc('month', {Order Date})", abstractType: date}
      Next Day: {expression: "date_add('day', 1, {Order Date})", abstractType: date}
      Same Day: {expression: "{Order Date}", abstractType: date}
      Order Label: {expression: "concat('#', {Id})", abstractType: string}
dimensions:
  Order Month: {dataObject: Orders, column: Order Month, resultType: date}
  Next Day: {dataObject: Orders, column: Next Day, resultType: date}
  Same Day: {dataObject: Orders, column: Same Day, resultType: date}
  Order Label: {dataObject: Orders, column: Order Label, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum,
            resultType: float}
"""


def _sql(query: dict[str, object], dialect: str = "duckdb") -> str:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return CompilationPipeline().compile(QueryObject.model_validate(query), model, dialect).sql


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id VARCHAR, order_date DATE, amount DOUBLE)")
    con.execute(
        "INSERT INTO orders VALUES ('a', DATE '2026-08-15', 10), ('b', DATE '2026-08-31', 5)"
    )
    return con


@pytest.mark.parametrize(
    ("dimension", "values"),
    [
        ("Order Month", [datetime.date(2026, 8, 1)]),
        ("Next Day", [datetime.date(2026, 8, 16), datetime.date(2026, 9, 1)]),
    ],
)
def test_a_computed_date_column_returns_dates(
    con: duckdb.DuckDBPyConnection, dimension: str, values: list[datetime.date]
) -> None:
    sql = _sql({"select": {"dimensions": [dimension], "measures": ["Revenue"]}})
    got = sorted(row[0] for row in con.execute(sql).fetchall())
    assert got == values, sql
    assert all(type(v) is datetime.date for v in got), got


def test_a_filter_compares_the_date(con: duckdb.DuckDBPyConnection) -> None:
    sql = _sql(
        {
            "select": {"dimensions": ["Next Day"], "measures": ["Revenue"]},
            "where": [{"field": "Next Day", "op": "=", "value": "2026-09-01"}],
        }
    )
    assert con.execute(sql).fetchall() == [(datetime.date(2026, 9, 1), 5.0)], sql


@pytest.mark.parametrize(
    "dialect",
    ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "mysql", "postgres", "snowflake"],
)
def test_every_dialect_casts_it(dialect: str) -> None:
    sql = _sql({"select": {"dimensions": ["Order Month", "Next Day"], "measures": []}}, dialect)
    date_type = "Nullable\\(Date\\)" if dialect == "clickhouse" else "DATE"
    for alias in ("Order Month", "Next Day"):
        assert re.search(rf"AS {date_type}\) AS [`\"]{alias}[`\"]", sql), sql


@pytest.mark.parametrize("dimension", ["Same Day", "Order Label"])
def test_a_column_already_of_its_type_is_not_cast(dimension: str) -> None:
    sql = _sql({"select": {"dimensions": [dimension], "measures": []}})
    assert "CAST(" not in sql, sql
