"""The base object reaches the query's dimensions, not only its measures.

Orders -> Customers -> Regions, one join each. Grouped by the month of the
orders, measures on the customers and their regions are one join tree seen
from its dimension end: based on Orders, each customer's credit is counted
once per month, however many orders it placed.

=========  ======  ======  ===========  ==========
order      date    amount  customer     credit
=========  ======  ======  ===========  ==========
``o1``     Jan 5   1       ``k1``       10
``o2``     Jan 9   2       ``k1``       10
``o3``     Feb 1   4       ``k2``       20
=========  ======  ======  ===========  ==========

Both customers are in ``r1``, population 1000.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: base_reaches_dimensions
dataObjects:
  Regions:
    code: regions
    columns:
      Region Id: {code: id, abstractType: string, primaryKey: true}
      Population: {code: population, abstractType: float}
  Customers:
    code: customers
    columns:
      Customer Id: {code: id, abstractType: string, primaryKey: true}
      Customer Region Id: {code: region_id, abstractType: string}
      Credit: {code: credit, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region Id]
        columnsTo: [Region Id]
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Order Customer Id: {code: customer_id, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer Id]
        columnsTo: [Customer Id]
dimensions:
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
measures:
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Total Population:
    columns: [{dataObject: Regions, column: Population}]
    aggregation: sum
    resultType: float
  Total Amount:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
metrics:
  Prior Credit:
    type: period_over_period
    expression: '{[Total Credit]}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Prior Population:
    type: period_over_period
    expression: '{[Total Population]}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
"""

JAN, FEB = date(2024, 1, 1), date(2024, 2, 1)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions AS SELECT * FROM (VALUES ('r1', 1000.0)) t(id, population)")
    con.execute(
        "CREATE TABLE customers AS SELECT * FROM"
        " (VALUES ('k1', 'r1', 10.0), ('k2', 'r1', 20.0)) t(id, region_id, credit)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES"
        " ('o1', 'k1', DATE '2024-01-05', 1.0), ('o2', 'k1', DATE '2024-01-09', 2.0),"
        " ('o3', 'k2', DATE '2024-02-01', 4.0)) t(id, customer_id, order_date, amount)"
    )
    return con


def _rows(con: duckdb.DuckDBPyConnection, measures: list[str]) -> tuple[str, list[tuple]]:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject(
        **{
            "select": {"dimensions": ["Order Month"], "measures": measures},
            "orderBy": [{"field": "Order Month"}],
        }
    )
    compiled = CompilationPipeline().compile(query, model, "duckdb")
    assert compiled.explain is not None
    return compiled.explain.planner, con.execute(compiled.sql).fetchall()


def _d(*values: float | None) -> tuple[Decimal | None, ...]:
    return tuple(None if v is None else Decimal(f"{v:.2f}") for v in values)


@pytest.mark.parametrize(
    ("measures", "jan", "feb"),
    [
        (["Total Credit", "Total Population"], _d(10, 1000), _d(20, 1000)),
        (["Prior Credit", "Total Population"], _d(None, 1000), _d(10, 1000)),
    ],
    ids=["plain", "prior-credit"],
)
def test_measure_sources_one_reaches_the_other(
    con: duckdb.DuckDBPyConnection, measures: list[str], jan: tuple, feb: tuple
) -> None:
    """Customers reaches Regions, so the two measures are one fact, and the
    base moves to the Orders that reach the month. It stayed on Customers,
    since there were two measure sources: 'Orders ... cannot be reached'."""
    planner, rows = _rows(con, measures)
    assert planner == "Star Schema"
    assert rows == [(JAN, *jan), (FEB, *feb)]


@pytest.mark.parametrize(
    ("measures", "jan", "feb"),
    [
        (["Total Credit", "Total Population", "Total Amount"], _d(10, 1000, 3), _d(20, 1000, 4)),
        (["Prior Credit", "Total Population", "Total Amount"], _d(None, 1000, 3), _d(10, 1000, 4)),
        (["Prior Credit", "Prior Population", "Total Amount"], _d(None, None, 3), _d(10, 1000, 4)),
    ],
    ids=["plain", "prior-credit", "prior-both"],
)
def test_a_tie_goes_to_the_source_reaching_the_query(
    con: duckdb.DuckDBPyConnection, measures: list[str], jan: tuple, feb: tuple
) -> None:
    """Customers and Orders have one join each. Customers, first by name,
    was the base: it cannot reach Orders, so one join tree was planned as
    independent facts, and a period-over-period metric refused them."""
    planner, rows = _rows(con, measures)
    assert planner == "Star Schema"
    assert rows == [(JAN, *jan), (FEB, *feb)]
