"""An average's total reads the argument a dialect's rewrite of it still holds.

``total: true`` on an average re-aggregates it from ``SUM`` and ``COUNT``
helpers built from the projected column's aggregate. A dialect that rewrites
the average leaves no ``AVG`` there - an exact division on Dremio and
Databricks, integer arithmetic on DuckDB, a division of an exact
``SUM(DISTINCT x)`` for a float DISTINCT average on Snowflake - so the helpers
fell back to ``SUM(1)`` and ``COUNT(1)``, and the total of 2, 4 and 6 read 1.
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: avg_total_after_rewrite
dataObjects:
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Region: {code: region, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
dimensions:
  Region: {dataObject: Orders, column: Region, resultType: string}
measures:
  Int Mean: {columns: [{dataObject: Orders, column: Quantity}], aggregation: avg,
             resultType: int, total: true}
  Float Mean: {columns: [{dataObject: Orders, column: Quantity}], aggregation: avg,
               resultType: float, total: true}
  Distinct Mean: {columns: [{dataObject: Orders, column: Quantity}], aggregation: avg,
                  resultType: float, distinct: true, total: true}
"""

DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]


def _sql(measure: str, dialect: str) -> str:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Region"], "measures": [measure]}}
    )
    return CompilationPipeline().compile(query, model, dialect).sql


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("measure", ["Int Mean", "Float Mean"])
def test_the_helpers_read_the_column(measure: str, dialect: str) -> None:
    sql = _sql(measure, dialect)
    assert "SUM(1)" not in sql and "COUNT(1)" not in sql, sql
    assert "quantity" in sql.split("FROM")[0], sql


@pytest.mark.parametrize("measure", ["Int Mean", "Float Mean", "Distinct Mean"])
def test_the_total_of_2_4_and_6_is_4(measure: str) -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id VARCHAR, region VARCHAR, quantity BIGINT)")
    con.execute("INSERT INTO orders VALUES ('a', 'x', 2), ('b', 'x', 4), ('c', 'y', 6)")
    rows = con.execute(_sql(measure, "duckdb")).fetchall()
    assert sorted(rows) == [("x", 4), ("y", 4)], rows


def test_a_distinct_average_totals_the_distinct_values() -> None:
    """2 twice, 4 and 6: the distinct values average 4. The helpers summed and
    counted every row, so the total read 3.5; the average is computed apart."""
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id VARCHAR, region VARCHAR, quantity BIGINT)")
    con.execute(
        "INSERT INTO orders VALUES ('a', 'x', 2), ('b', 'x', 4), ('c', 'y', 6), ('d', 'y', 2)"
    )
    rows = con.execute(_sql("Distinct Mean", "duckdb")).fetchall()
    assert sorted(rows) == [("x", 4), ("y", 4)], rows


@pytest.mark.parametrize(
    ("dialect", "widened"),
    [
        ("clickhouse", 'SUM(toDecimal128("Orders"."quantity", 0))'),
        ("dremio", 'SUM(CAST("Orders"."quantity" AS DECIMAL(38, 0)))'),
    ],
)
def test_the_sum_helper_keeps_the_widened_accumulator(dialect: str, widened: str) -> None:
    sql = _sql("Int Mean", dialect)
    assert f'{widened} AS "Int Mean__sum"' in sql, sql
