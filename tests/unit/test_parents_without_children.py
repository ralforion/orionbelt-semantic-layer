"""A parent measure keeps the parents without children, whatever else is selected.

A query reading an order measure is based on the orders, and reaches the
customers through them: a customer without orders was in no row, so
``Customers Count`` was 3 alone and 2 beside ``Revenue``. Now those customers
are added as rows of their own (never joined to the orders, so nothing is
multiplied), and an order without a customer stays a row too, so every measure
adds up to what it reads alone. Grouped by an order column, the customers
without orders are its NULL group; a WHERE that no such customer passes leaves
them out.
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
      Category: {code: category, abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Order Product ID: {code: product_id, abstractType: int}
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
  Tier: {dataObject: Customers, column: Tier, resultType: string}
  Category: {dataObject: Products, column: Category, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Credit Plus One:
    expression: 'COALESCE({[Customers].[Credit]}, 0) + 1'
    aggregation: sum
    resultType: float
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, tier VARCHAR, credit DOUBLE)")
    # Carol has no orders.
    con.execute("INSERT INTO customers VALUES (1, 'gold', 10), (2, 'silver', 20), (3, 'gold', 40)")
    con.execute("CREATE TABLE products (id INTEGER, category VARCHAR)")
    con.execute("INSERT INTO products VALUES (1, 'tools'), (2, 'toys')")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, product_id INTEGER, amount DOUBLE)"
    )
    # Alice buys tools twice, Bob toys once; order 4 has no customer.
    con.execute(
        "INSERT INTO orders VALUES (1, 1, 1, 100), (2, 1, 1, 50), (3, 2, 2, 70), (4, NULL, 1, 5)"
    )
    return con


def _sql(query: dict) -> str:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    return CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    return sorted(
        (
            tuple(float(v) if isinstance(v, int | float) else v for v in row)
            for row in con.execute(sql).fetchall()
        ),
        key=str,
    )


def test_the_same_with_or_without_an_order_measure(con: duckdb.DuckDBPyConnection) -> None:
    alone = _rows(con, _sql({"select": {"measures": ["Customers Count", "Total Credit"]}}))
    beside = _rows(
        con, _sql({"select": {"measures": ["Revenue", "Customers Count", "Total Credit"]}})
    )
    assert alone == [(3.0, 70.0)]
    assert beside == [(225.0, 3.0, 70.0)]


def test_both_kinds_of_missing_link_are_rows(con: duckdb.DuckDBPyConnection) -> None:
    """Carol is in gold; the order without a customer is the NULL tier."""
    sql = _sql({"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Customers Count"]}})
    assert _rows(con, sql) == [
        ("gold", 150.0, 2.0),
        ("silver", 70.0, 1.0),
        (None, 5.0, 0.0),
    ]


def test_grouped_by_an_order_column(con: duckdb.DuckDBPyConnection) -> None:
    """Carol bought nothing, so she is the NULL category, and the credit adds up
    to what it reads alone (70)."""
    sql = _sql({"select": {"dimensions": ["Category"], "measures": ["Total Credit"]}})
    assert _rows(con, sql) == [("tools", 10.0), ("toys", 20.0), (None, 40.0)]


def test_a_where_no_such_customer_passes_leaves_them_out(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Carol's category is NULL, which ``tools`` rejects: no rows are added."""
    sql = _sql(
        {
            "select": {"dimensions": ["Category"], "measures": ["Total Credit"]},
            "where": [{"field": "Category", "op": "=", "value": "tools"}],
        }
    )
    assert "NOT EXISTS" not in sql
    assert _rows(con, sql) == [("tools", 10.0)]


def test_one_cte_has_every_group() -> None:
    """The customers' CTE reads every order row and the customers without one,
    so it drives the query; no list of groups is collected from the others."""
    sql = _sql({"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Total Credit"]}})
    assert "__ob_groups" not in sql
    assert 'FROM "__ob_dedup_0" AS "__ob_dedup_0"\nLEFT JOIN "__ob_main"' in sql


def test_an_expression_cannot_give_a_missing_customer_a_value(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """``COALESCE(credit, 0) + 1`` is 1 over a NULL credit. Gating the column
    inside the aggregate still added that 1 for the order without a customer:
    the groups totalled 74 against 73 alone. The aggregate reads the rows that
    have a customer only; the order's group comes from the rows, valueless."""
    alone = _rows(con, _sql({"select": {"measures": ["Credit Plus One"]}}))
    grouped = _rows(
        con, _sql({"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Credit Plus One"]}})
    )
    assert alone == [(73.0,)]
    assert grouped == [("gold", 150.0, 52.0), ("silver", 70.0, 21.0), (None, 5.0, None)]
