"""The base object reaches the query's dimensions, not only its measures.

Orders -> Customers -> Regions, one join each. Grouped by the month of the
orders, measures on the customers and their regions are one join tree seen
from its dimension end: based on Orders, each customer's credit is counted
once per month, however many orders it placed, and a customer without orders
is the NULL month's.

=========  ======  ======  ===========  ==========  ======  ==========
order      date    amount  customer     credit      region  population
=========  ======  ======  ===========  ==========  ======  ==========
``o1``     Jan 5   1       ``k1``       10          ``r1``  1000
``o2``     Jan 9   2       ``k1``       10          ``r1``  1000
``o3``     Feb 1   4       ``k2``       20          ``r1``  1000
*(none)*                   ``k3``       40          ``r2``  500
=========  ======  ======  ===========  ==========  ======  ==========
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.compiler.resolution import ResolutionError
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
  Region: {dataObject: Regions, column: Region Id, resultType: string}
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
    con.execute(
        "CREATE TABLE regions AS SELECT * FROM"
        " (VALUES ('r1', 1000.0), ('r2', 500.0)) t(id, population)"
    )
    con.execute(
        "CREATE TABLE customers AS SELECT * FROM (VALUES"
        " ('k1', 'r1', 10.0), ('k2', 'r1', 20.0), ('k3', 'r2', 40.0)) t(id, region_id, credit)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES"
        " ('o1', 'k1', DATE '2024-01-05', 1.0), ('o2', 'k1', DATE '2024-01-09', 2.0),"
        " ('o3', 'k2', DATE '2024-02-01', 4.0)) t(id, customer_id, order_date, amount)"
    )
    return con


def _compile(
    measures: list[str],
    dimensions: list[str],
    grouping: str | None = None,
    yaml_text: str = MODEL_YAML,
) -> CompilationResult:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query: dict = {"select": {"dimensions": dimensions, "measures": measures}}
    if dimensions:
        query["orderBy"] = [{"field": dimensions[0]}]
    if grouping:
        query["grouping"] = grouping
    return CompilationPipeline().compile(QueryObject(**query), model, "duckdb")


def _rows(
    con: duckdb.DuckDBPyConnection,
    measures: list[str],
    dimensions: list[str] | None = None,
    grouping: str | None = None,
    yaml_text: str = MODEL_YAML,
) -> tuple[str, list[tuple]]:
    compiled = _compile(measures, dimensions or [], grouping, yaml_text)
    assert compiled.explain is not None
    return compiled.explain.planner, con.execute(compiled.sql).fetchall()


def _d(*values: float | None) -> tuple[Decimal | None, ...]:
    return tuple(None if v is None else Decimal(f"{v:.2f}") for v in values)


@pytest.mark.parametrize(
    ("measures", "jan", "feb", "none"),
    [
        (["Total Credit", "Total Population"], _d(10, 1000), _d(20, 1000), _d(40, 500)),
        (["Prior Credit", "Total Population"], _d(None, 1000), _d(10, 1000), _d(None, 500)),
    ],
    ids=["plain", "prior-credit"],
)
def test_measure_sources_one_reaches_the_other(
    con: duckdb.DuckDBPyConnection, measures: list[str], jan: tuple, feb: tuple, none: tuple
) -> None:
    """Customers reaches Regions, so the two measures are one fact, and the
    base moves to the Orders that reach the month. It stayed on Customers,
    since there were two measure sources: 'Orders ... cannot be reached'."""
    planner, rows = _rows(con, measures, ["Order Month"])
    assert planner == "Star Schema"
    assert rows == [(JAN, *jan), (FEB, *feb), (None, *none)]


@pytest.mark.parametrize(
    ("measures", "jan", "feb", "none"),
    [
        (
            ["Total Credit", "Total Population", "Total Amount"],
            _d(10, 1000, 3),
            _d(20, 1000, 4),
            _d(40, 500, None),
        ),
        (
            ["Prior Credit", "Total Population", "Total Amount"],
            _d(None, 1000, 3),
            _d(10, 1000, 4),
            _d(None, 500, None),
        ),
        (
            ["Prior Credit", "Prior Population", "Total Amount"],
            _d(None, None, 3),
            _d(10, 1000, 4),
            _d(None, None, None),
        ),
    ],
    ids=["plain", "prior-credit", "prior-both"],
)
def test_a_tie_goes_to_the_source_reaching_the_query(
    con: duckdb.DuckDBPyConnection, measures: list[str], jan: tuple, feb: tuple, none: tuple
) -> None:
    """Customers and Orders have one join each. Customers, first by name,
    was the base: it cannot reach Orders, so one join tree was planned as
    independent facts, which left k3 out, and a period-over-period metric
    refused them."""
    planner, rows = _rows(con, measures, ["Order Month"])
    assert planner == "Star Schema"
    assert rows == [(JAN, *jan), (FEB, *feb), (None, *none)]


@pytest.mark.parametrize(
    "measures",
    [
        ["Total Credit", "Total Population"],
        ["Total Credit", "Total Population", "Total Amount"],
    ],
    ids=["one-fact", "tie"],
)
def test_under_rollup_several_parents_keep_the_base(measures: list[str]) -> None:
    """A star on Orders cannot add back the customers and the regions without
    orders under ROLLUP: k3 and r2 were left out, a total of 30 / 1000. The
    query is refused, as before, rather than answered without them."""
    with pytest.raises(ResolutionError):
        _compile(measures, ["Order Month"], "rollup")


def test_a_rollup_by_region_keeps_its_union(con: duckdb.DuckDBPyConnection) -> None:
    """Grouped by region, Customers reaches the query, and its union keeps
    r2: on Orders the star dropped it, a total of 30 / 1000."""
    planner, rows = _rows(
        con, ["Total Credit", "Total Population", "Total Amount"], ["Region"], "rollup"
    )
    assert planner == "CFL"
    assert rows == [
        (None, *_d(70, 1500, 7), 1),
        ("r1", *_d(30, 1000, 7), 0),
        ("r2", *_d(40, 500, None), 0),
    ]


def test_a_many_to_many_join_does_not_count_as_reach(con: duckdb.DuckDBPyConnection) -> None:
    """Orders -> Customers declared many-to-many: Orders reaches Customers only
    by multiplying the orders, so the tie stays with Customers and its union.
    Based on Orders, the query raised a fanout error."""
    yaml_text = MODEL_YAML.replace(
        "      - joinType: many-to-one\n        joinTo: Customers",
        "      - joinType: many-to-many\n        joinTo: Customers",
    )
    assert yaml_text != MODEL_YAML
    planner, rows = _rows(con, ["Total Credit", "Total Amount"], yaml_text=yaml_text)
    assert planner == "CFL"
    assert rows == [_d(70, 7)]
