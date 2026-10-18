"""A period-over-period query keeps the rows without a period.

By order month, a customer without orders and an order without a date have
no month: the plain query shows them as the NULL month. The PoP query read
its rows by joining them to the date spine, so both were dropped; and the
customers without orders were never added to its rows at all. Now its rows
are the plain query's: the source rows with the spine joined to them, a NULL
month for a row without a date, and the parents without children as rows of
their own. A NULL month has no prior period.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Tier: {code: tier, abstractType: string}
      Credit: {code: credit, abstractType: float}
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: int, primaryKey: true}
      Price: {code: price, abstractType: float}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Order Product ID: {code: product_id, abstractType: int}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Order Product ID]
        columnsTo: [Product ID]
dimensions:
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
  Tier: {dataObject: Customers, column: Tier, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Total Price:
    columns: [{dataObject: Products, column: Price}]
    aggregation: sum
    resultType: float
metrics:
  Prior Revenue:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Prior Credit:
    type: period_over_period
    expression: '{[Total Credit]}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, tier VARCHAR, credit DOUBLE)")
    # Carol has no orders.
    con.execute("INSERT INTO customers VALUES (1, 'gold', 10), (2, 'silver', 20), (3, 'gold', 40)")
    con.execute("CREATE TABLE products (id INTEGER, price DOUBLE)")
    # Product 3 is never ordered.
    con.execute("INSERT INTO products VALUES (1, 3), (2, 4), (3, 8)")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, product_id INTEGER,"
        " order_date DATE, amount DOUBLE)"
    )
    # Order 4 has no date.
    con.execute(
        "INSERT INTO orders VALUES (1, 1, 1, DATE '2026-01-05', 100),"
        " (2, 1, 1, DATE '2026-02-05', 50), (3, 2, 2, DATE '2026-02-07', 70),"
        " (4, 2, 2, NULL, 9)"
    )
    return con


def _rows(con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple[object, ...]]:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return sorted(
        (
            tuple(
                float(v)
                if isinstance(v, int | float)
                else (str(v)[:7] if hasattr(v, "year") else v)
                for v in row
            )
            for row in con.execute(sql).fetchall()
        ),
        key=str,
    )


def test_the_rows_are_the_plain_querys(con: duckdb.DuckDBPyConnection) -> None:
    """The NULL month carries the order without a date (9) and the credit of
    Bob through it and of Carol (60); its prior period is unknown."""
    plain = _rows(
        con, {"select": {"dimensions": ["Order Month"], "measures": ["Revenue", "Total Credit"]}}
    )
    pop = _rows(
        con,
        {
            "select": {
                "dimensions": ["Order Month"],
                "measures": ["Revenue", "Total Credit", "Prior Revenue", "Prior Credit"],
            }
        },
    )
    assert plain == [("2026-01", 100.0, 10.0), ("2026-02", 120.0, 30.0), (None, 9.0, 60.0)]
    assert [r[:3] for r in pop] == plain
    assert [r[3:] for r in pop] == [(None, None), (100.0, 10.0), (None, None)]


def test_an_order_without_a_date_is_kept_alone(con: duckdb.DuckDBPyConnection) -> None:
    """Without any parent measure: the order without a date was dropped."""
    rows = _rows(
        con, {"select": {"dimensions": ["Order Month"], "measures": ["Revenue", "Prior Revenue"]}}
    )
    assert rows == [("2026-01", 100.0, None), ("2026-02", 120.0, 100.0), (None, 9.0, None)]


def test_several_parents_and_a_where(con: duckdb.DuckDBPyConnection) -> None:
    """Customers and products both read through the orders: Carol and the
    never-ordered product are the NULL month. ``Tier = gold OR Tier notset``
    keeps Carol, Alice, the order without a date (silver) is gone."""
    query = {
        "select": {
            "dimensions": ["Order Month"],
            "measures": ["Total Credit", "Total Price", "Prior Revenue"],
        },
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Tier", "op": "=", "value": "gold"},
                    {"field": "Tier", "op": "notset"},
                ],
            }
        ],
    }
    assert _rows(con, query) == [
        ("2026-01", 10.0, 3.0, None),
        ("2026-02", 10.0, 3.0, 100.0),
        (None, 40.0, 8.0, None),
    ]


def test_an_or_group_beside_another_filter_keeps_its_parentheses(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """``Tier = gold`` and ``(no date OR Tier = silver)`` hold for no order.
    The PoP source joined its filters with a bare AND, so it read
    ``gold AND no date OR silver`` and returned Bob's orders."""
    query = {
        "select": {"dimensions": ["Order Month"], "measures": ["Revenue", "Prior Revenue"]},
        "where": [
            {"field": "Tier", "op": "=", "value": "gold"},
            {
                "logic": "or",
                "filters": [
                    {"field": "Order Month", "op": "notset"},
                    {"field": "Tier", "op": "=", "value": "silver"},
                ],
            },
        ],
    }
    assert _rows(con, query) == []
