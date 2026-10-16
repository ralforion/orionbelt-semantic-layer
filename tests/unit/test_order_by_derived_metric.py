"""ORDER BY a derived metric sorts by the metric's own column.

A derived metric's expression is a formula over its components' aliases
(``"Revenue" / NULLIF("Order Count", 0)``). Emitted as the sort key, it named
columns the query does not have unless every component was selected too, and
the database refused the statement ("Referenced column Revenue not found").
Even with the components selected it bound only where an engine reads output
aliases inside an ORDER BY expression.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect import DialectRegistry
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
      Customer Name: {code: name, abstractType: string}
      Credit: {code: credit, abstractType: float}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: int, primaryKey: true}
      Return Customer ID: {code: customer_id, abstractType: int}
      Refund: {code: refund, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Customer: {dataObject: Customers, column: Customer Name, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    aggregation: count
    resultType: int
  Refunds:
    columns: [{dataObject: Returns, column: Refund}]
    aggregation: sum
    resultType: float
  Grand Total:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    total: true
  All Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    filterContext: {mode: FIXED}
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
metrics:
  Average Order:
    expression: "{[Revenue]} / {[Order Count]}"
  Refund Rate:
    expression: "{[Refunds]} / {[Revenue]}"
  Rounded Average:
    expression: "{[Average Order]}"
    dataType: integer
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob'), (3, 'Carol')")
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE)")
    # Average order: Alice 50, Bob 300, Carol 100.
    con.execute("INSERT INTO orders VALUES (1, 1, 40), (2, 1, 60), (3, 2, 300), (4, 3, 100)")
    con.execute("CREATE TABLE returns (id INTEGER, customer_id INTEGER, refund DOUBLE)")
    # Refund rate: Alice 0.5, Bob 0.1, Carol 0.2.
    con.execute("INSERT INTO returns VALUES (1, 1, 50), (2, 2, 30), (3, 3, 20)")
    return con


def _sql(query: dict, dialect: str = "duckdb") -> str:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    return CompilationPipeline().compile(QueryObject(**query), model, dialect).sql


AVERAGE_ORDER_DESC = {
    "select": {"dimensions": ["Customer"], "measures": ["Average Order"]},
    "orderBy": [{"field": "Average Order", "direction": "desc"}],
    "limit": 2,
}


def test_sorts_without_its_components_selected(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(_sql(AVERAGE_ORDER_DESC)).fetchall()
    assert [(name, float(value)) for name, value in rows] == [("Bob", 300.0), ("Carol", 100.0)]


def test_sorts_across_independent_facts(con: duckdb.DuckDBPyConnection) -> None:
    """Refunds and Revenue come from two facts, joined in a union."""
    sql = _sql(
        {
            "select": {"dimensions": ["Customer"], "measures": ["Refund Rate"]},
            "orderBy": [{"field": "Refund Rate", "direction": "asc"}],
        }
    )
    assert [name for name, _ in con.execute(sql).fetchall()] == ["Bob", "Carol", "Alice"]


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
def test_the_sort_key_is_the_metric_column(dialect: str) -> None:
    sql = _sql(AVERAGE_ORDER_DESC, dialect)
    order_by = sql[sql.rindex("ORDER BY") :]
    assert "Average Order" in order_by
    assert "Revenue" not in order_by and "Order Count" not in order_by


@pytest.mark.parametrize(
    "companion",
    ["Grand Total", "All Revenue", "Total Credit"],
    ids=["total-wrap", "filter-context", "grain-dedup"],
)
def test_a_metric_over_the_sort_metric_does_not_take_its_place(companion: str) -> None:
    """Rounded Average's expression is the bare alias "Average Order". A wrapper
    rewriting the ORDER BY matched it, and sorted by the rounded column: 50.1,
    50.2 and 50.3 all round to 50, so the tie broke on the customer instead."""
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, name VARCHAR, credit DOUBLE)")
    con.execute("INSERT INTO customers VALUES (1, 'Alice', 1), (2, 'Bob', 2), (3, 'Carol', 3)")
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE)")
    con.execute("INSERT INTO orders VALUES (1, 1, 50.1), (2, 2, 50.2), (3, 3, 50.3)")
    sql = _sql(
        {
            "select": {
                "dimensions": ["Customer"],
                "measures": ["Average Order", "Rounded Average", companion],
            },
            "orderBy": [
                {"field": "Average Order", "direction": "desc"},
                {"field": "Customer", "direction": "asc"},
            ],
            "limit": 2,
        }
    )
    assert [row[0] for row in con.execute(sql).fetchall()] == ["Carol", "Bob"]
