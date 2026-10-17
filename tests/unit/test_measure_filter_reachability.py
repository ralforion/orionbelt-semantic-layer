"""A measure filter on another data object is applied to the rows it gates.

A measure filter is evaluated on the row of the value it gates
(``SUM(CASE WHEN <filter> THEN value END)``). Base selection used to read only
the measure's own columns, so a Customers measure filtered on an Orders column
was planned from Customers, and the SQL named ``"orders"`` in a FROM that never
joins it: the database refused it, after an ``OUT_OF_SCOPE_TABLE`` warning.

Now the filter's objects are on the measure's row. A Customers value read
through Orders repeats per order and is deduplicated per customer; its filter
reads Orders, so it is applied to the rows *before* deduplicating - in the
DISTINCT it would keep a row per order. A filter on the measure's own object,
or one with nothing to deduplicate, stays a ``CASE WHEN`` in the aggregate.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.grain_dedup import GrainDedupUnsupportedError
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Regions:
    code: regions
    schema: main
    columns:
      Region ID: {code: id, abstractType: int, primaryKey: true}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Customer Region ID: {code: region_id, abstractType: int}
      Tier: {code: tier, abstractType: string}
      Credit: {code: credit, abstractType: float}
    # One join each for Customers and Orders: the "most joins" tie-break then
    # chose Customers, from which the filter's Orders cannot be reached.
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
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
      Order Date: {code: order_date, abstractType: date}
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
measures:
  Credit With Orders:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Orders, column: Order ID}
        operator: set
  Credit Buying Tools:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Products, column: Category}
        operator: equals
        values: [{dataType: string, valueString: tools}]
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Gold Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Customers, column: Tier}
        operator: equals
        values: [{dataType: string, valueString: gold}]
  Tiers By Order Date:
    columns: [{dataObject: Customers, column: Tier}]
    aggregation: listagg
    resultType: string
    withinGroup:
      column: {dataObject: Orders, column: Order Date}
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions (id INTEGER)")
    con.execute("INSERT INTO regions VALUES (1)")
    con.execute(
        "CREATE TABLE customers (id INTEGER, region_id INTEGER, tier VARCHAR, credit DOUBLE)"
    )
    # Carol has no orders.
    con.execute(
        "INSERT INTO customers VALUES (1, 1, 'gold', 10), (2, 1, 'silver', 20), (3, 1, 'gold', 40)"
    )
    con.execute("CREATE TABLE products (id INTEGER, category VARCHAR)")
    con.execute("INSERT INTO products VALUES (1, 'tools'), (2, 'toys')")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, product_id INTEGER,"
        " amount DOUBLE, order_date DATE)"
    )
    # Alice buys tools twice, Bob toys once.
    con.execute(
        "INSERT INTO orders VALUES (1, 1, 1, 100, DATE '2026-01-01'),"
        " (2, 1, 1, 50, DATE '2026-01-02'), (3, 2, 2, 70, DATE '2026-01-03')"
    )
    return con


def _sql(query: dict) -> str:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    return CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    return sorted(
        (tuple(float(v) if isinstance(v, int | float) else v for v in row))
        for row in con.execute(sql).fetchall()
    )


def test_credit_of_customers_with_orders(con: duckdb.DuckDBPyConnection) -> None:
    """Alice and Bob each once (10 + 20), however many orders; not Carol."""
    sql = _sql({"select": {"measures": ["Credit With Orders"]}})
    assert _rows(con, sql) == [(30.0,)]


def test_by_a_dimension(con: duckdb.DuckDBPyConnection) -> None:
    sql = _sql({"select": {"dimensions": ["Tier"], "measures": ["Credit With Orders"]}})
    assert _rows(con, sql) == [("gold", 10.0), ("silver", 20.0)]


def test_a_filter_two_joins_away(con: duckdb.DuckDBPyConnection) -> None:
    """Products is reached through Orders: Alice bought tools, twice."""
    sql = _sql({"select": {"measures": ["Credit Buying Tools"]}})
    expected = (
        "SELECT SUM(credit) FROM (SELECT DISTINCT c.id, c.credit FROM orders o"
        " JOIN customers c ON c.id = o.customer_id JOIN products p ON p.id = o.product_id"
        " WHERE p.category = 'tools')"
    )
    assert _rows(con, sql) == _rows(con, expected) == [(10.0,)]


def test_a_measure_without_the_filter_is_not_gated(con: duckdb.DuckDBPyConnection) -> None:
    """Gated rows are the gated measure's own: Total Credit beside it reads the
    customers with orders (the query is read through Orders), unfiltered."""
    sql = _sql({"select": {"measures": ["Credit Buying Tools", "Total Credit"]}})
    assert _rows(con, sql) == [(10.0, 30.0)]


def test_a_filter_the_measure_reaches_still_joins(con: duckdb.DuckDBPyConnection) -> None:
    """Orders reaches Customers, so the query is based on Orders and the tier
    filter joins Customers. Under the same tie it was based on Customers and
    the gated Orders amount named a table the FROM did not have."""
    sql = _sql({"select": {"measures": ["Gold Revenue"]}})
    assert _rows(con, sql) == [(150.0,)]


def test_a_sort_key_on_another_object_is_still_refused() -> None:
    """A sort key rides into the aggregate, so it cannot be applied before."""
    with pytest.raises(GrainDedupUnsupportedError, match="withinGroup"):
        _sql({"select": {"measures": ["Tiers By Order Date", "Gold Revenue"]}})
