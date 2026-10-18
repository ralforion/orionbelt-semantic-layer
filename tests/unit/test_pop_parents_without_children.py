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


SHARED_YAML = """
version: 1.0
dataObjects:
  Regions:
    code: regions
    schema: main
    columns:
      Region ID: {code: id, abstractType: int, primaryKey: true}
      Population: {code: population, abstractType: float}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Customer Region ID: {code: region_id, abstractType: int}
      Customer Tier: {code: tier, abstractType: string}
      Credit: {code: credit, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
  Channels:
    code: channels
    schema: main
    columns:
      Channel ID: {code: id, abstractType: int, primaryKey: true}
  # Orders has two joins, so it outranks Customers as the query's base.
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Order Channel ID: {code: channel_id, abstractType: int}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Channels
        columnsFrom: [Order Channel ID]
        columnsTo: [Channel ID]
dimensions:
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
  Customer Tier: {dataObject: Customers, column: Customer Tier, resultType: string}
measures:
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Total Population:
    columns: [{dataObject: Regions, column: Population}]
    aggregation: sum
    resultType: float
  Order Lines:
    expression: 'COALESCE({[Orders].[Amount]}, 0) + 1'
    aggregation: sum
    resultType: float
  Population With Orders:
    columns: [{dataObject: Regions, column: Population}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Orders, column: Order ID}
        operator: set
  Population With Big Orders:
    columns: [{dataObject: Regions, column: Population}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Orders, column: Amount}
        operator: gt
        values: [{dataType: float, valueFloat: 50}]
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
"""


@pytest.fixture
def shared() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions (id INTEGER, population DOUBLE)")
    con.execute("INSERT INTO regions VALUES (1, 100)")
    con.execute(
        "CREATE TABLE customers (id INTEGER, region_id INTEGER, tier VARCHAR, credit DOUBLE)"
    )
    # Carol (3) has no orders; all three live in region 1.
    con.execute(
        "INSERT INTO customers VALUES (1, 1, 'gold', 10), (2, 1, 'silver', 20), (3, 1, 'gold', 40)"
    )
    con.execute("CREATE TABLE channels (id INTEGER)")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, channel_id INTEGER,"
        " order_date DATE, amount DOUBLE)"
    )
    # Bob's order 3 has no date: Bob and Carol are both the NULL month.
    con.execute(
        "INSERT INTO orders VALUES (1, 1, NULL, DATE '2026-01-05', 100),"
        " (2, 2, NULL, DATE '2026-02-07', 70), (3, 2, NULL, NULL, 9)"
    )
    return con


def _shared_rows(
    con: duckdb.DuckDBPyConnection,
    measures: list[str],
    dimensions: tuple[str, ...] = ("Order Month",),
) -> list[tuple[object, ...]]:
    raw = YAML(typ="safe").load(SHARED_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    query = QueryObject(**{"select": {"dimensions": list(dimensions), "measures": measures}})
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
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


def test_a_child_measure_reads_nothing_on_a_parent_row(shared: duckdb.DuckDBPyConnection) -> None:
    """``COALESCE(amount, 0) + 1`` is not NULL where the order columns are:
    Carol, a customer without orders, added 1 to the NULL month's 10."""
    plain = _shared_rows(shared, ["Order Lines", "Total Credit"])
    pop = _shared_rows(shared, ["Order Lines", "Total Credit", "Prior Credit"])
    assert plain == [("2026-01", 101.0, 10.0), ("2026-02", 71.0, 20.0), (None, 10.0, 60.0)]
    assert [row[:3] for row in pop] == plain


def test_an_ancestor_shared_by_both_rows_counts_once(shared: duckdb.DuckDBPyConnection) -> None:
    """Region 1 is reached through Bob's undated order and through Carol. Each
    part of the union flagged its own first row of the region, so the NULL
    month summed its population twice: 200 instead of 100."""
    measures = ["Total Population", "Total Credit", "Order Lines"]
    plain = _shared_rows(shared, measures)
    pop = _shared_rows(shared, [*measures, "Prior Credit"])
    assert plain == [
        ("2026-01", 100.0, 10.0, 101.0),
        ("2026-02", 100.0, 20.0, 71.0),
        (None, 100.0, 60.0, 10.0),
    ]
    assert [row[:4] for row in pop] == plain


def test_a_filtered_ancestor_measure_reads_a_row_its_filter_keeps(
    shared: duckdb.DuckDBPyConnection,
) -> None:
    """The region's first row in a month was chosen before the measure's
    filter was read: a first row failing it left the region reading nothing.
    200 small undated orders fail ``Amount > 50``, the 60 inserted after them
    passes; the NULL month read NULL instead of 100. A first row of Carol's,
    without orders, fails ``Order ID set`` the same way."""
    shared.execute("INSERT INTO orders SELECT 10 + i, 2, NULL, NULL, 9 FROM range(200) t(i)")
    shared.execute("INSERT INTO orders VALUES (4, 1, NULL, NULL, 60)")
    measures = ["Population With Big Orders", "Population With Orders", "Total Population"]
    plain = _shared_rows(shared, measures)
    pop = _shared_rows(shared, [*measures, "Prior Credit"])
    assert plain == [
        ("2026-01", 100.0, 100.0, 100.0),
        ("2026-02", 100.0, 100.0, 100.0),
        (None, 100.0, 100.0, 100.0),
    ]
    assert [row[:4] for row in pop] == plain


def test_an_ancestor_measure_reads_only_its_own_parent_rows(
    shared: duckdb.DuckDBPyConnection,
) -> None:
    """Carol, without orders, lives alone in region 2. Region 2 has no
    customer with orders, so its population is the regions' own row without
    children: NULL tier, as in the plain query. It was also read through
    Carol's row, under gold, so it counted twice: (NULL, gold) read 300."""
    shared.execute("INSERT INTO regions VALUES (2, 200)")
    shared.execute("UPDATE customers SET region_id = 2 WHERE id = 3")
    measures = ["Total Population", "Total Credit", "Order Lines"]
    dimensions = ("Order Month", "Customer Tier")
    plain = _shared_rows(shared, measures, dimensions)
    pop = _shared_rows(shared, [*measures, "Prior Credit"], dimensions)
    assert plain == [
        ("2026-01", "gold", 100.0, 10.0, 101.0),
        ("2026-02", "silver", 100.0, 20.0, 71.0),
        (None, "gold", None, 40.0, None),
        (None, "silver", 100.0, 20.0, 10.0),
        (None, None, 200.0, None, None),
    ]
    assert [row[:5] for row in pop] == plain
