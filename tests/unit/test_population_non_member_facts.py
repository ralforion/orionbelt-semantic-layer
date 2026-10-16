"""A ``population: dimension`` metric keeps the facts that belong to no member.

Stage 1 lists every member of ``per`` (every region) and joins it to the facts.
Facts with no member - a customer without a region, an order without a
customer - formed a group of their own in the query's rows, but the metric was
computed over the members only, so that row came back with every value NULL
and the facts' values were nowhere in the result. Now the stage-1 groups are
the members and the fact groups together: the row carries the facts' own
values, so the rows add up to the whole, and a filter on the member columns
removes it like any other.
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
  Regions:
    code: regions
    schema: main
    columns:
      Region ID: {code: id, abstractType: int, primaryKey: true}
      Region Name: {code: name, abstractType: string}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Customer Name: {code: name, abstractType: string}
      Customer Region ID: {code: region_id, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
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
dimensions:
  Region: {dataObject: Regions, column: Region Name, resultType: string}
  Customer: {dataObject: Customers, column: Customer Name, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    defaultValue: 0
metrics:
  Region Revenue:
    type: reaggregate
    measure: Revenue
    per: [Region]
    aggregation: sum
    population: dimension
  Customer Revenue:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: sum
    population: dimension
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions (id INTEGER, name VARCHAR)")
    # East has no customers.
    con.execute("INSERT INTO regions VALUES (1, 'North'), (2, 'South'), (3, 'East')")
    con.execute("CREATE TABLE customers (id INTEGER, name VARCHAR, region_id INTEGER)")
    # Eve has no region.
    con.execute("INSERT INTO customers VALUES (1, 'Alice', 1), (2, 'Bob', 2), (3, 'Eve', NULL)")
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE)")
    # Order 4 has no customer.
    con.execute("INSERT INTO orders VALUES (1, 1, 100), (2, 2, 200), (3, 3, 40), (4, NULL, 5)")
    return con


def _rows(con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple[object, ...]]:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return sorted(((row[0], float(row[1])) for row in con.execute(sql).fetchall()), key=str)


def test_facts_without_a_member_keep_their_values(con: duckdb.DuckDBPyConnection) -> None:
    """Eve's 40 and the orphan's 5 are the NULL region's, not lost; East is a
    member without facts and reads the default."""
    rows = _rows(con, {"select": {"dimensions": ["Region"], "measures": ["Region Revenue"]}})
    assert rows == [("East", 0.0), ("North", 100.0), ("South", 200.0), (None, 45.0)]


def test_the_rows_add_up_to_the_whole(con: duckdb.DuckDBPyConnection) -> None:
    rows = _rows(con, {"select": {"dimensions": ["Region"], "measures": ["Region Revenue"]}})
    total = con.execute("SELECT SUM(amount) FROM orders").fetchone()[0]
    assert sum(value for _, value in rows) == total


def test_a_member_filter_removes_the_group(con: duckdb.DuckDBPyConnection) -> None:
    rows = _rows(
        con,
        {
            "select": {"dimensions": ["Region"], "measures": ["Region Revenue"]},
            "where": [{"field": "Region", "op": "set"}],
        },
    )
    assert rows == [("East", 0.0), ("North", 100.0), ("South", 200.0)]


def test_an_orphan_fact_is_a_group_of_its_own(con: duckdb.DuckDBPyConnection) -> None:
    """Per customer, the order without a customer is the NULL customer."""
    rows = _rows(con, {"select": {"dimensions": ["Customer"], "measures": ["Customer Revenue"]}})
    assert rows == [("Alice", 100.0), ("Bob", 200.0), ("Eve", 40.0), (None, 5.0)]


SNAPSHOT_YAML = """
version: 1.0
dataObjects:
  Accounts:
    code: accounts
    schema: main
    columns:
      Account ID: {code: id, abstractType: int, primaryKey: true}
      Account Name: {code: name, abstractType: string}
  Snapshots:
    code: snapshots
    schema: main
    columns:
      Snapshot ID: {code: id, abstractType: int, primaryKey: true}
      Snapshot Account ID: {code: account_id, abstractType: int}
      Snapshot Date: {code: snap_date, abstractType: date}
      Balance: {code: balance, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Accounts
        columnsFrom: [Snapshot Account ID]
        columnsTo: [Account ID]
dimensions:
  Account: {dataObject: Accounts, column: Account Name, resultType: string}
  Snapshot Date: {dataObject: Snapshots, column: Snapshot Date, resultType: date}
measures:
  Last Balance:
    columns: [{dataObject: Snapshots, column: Balance}]
    aggregation: last
    withinGroup:
      column: {dataObject: Snapshots, column: Snapshot Date}
metrics:
  Closing Balance:
    type: reaggregate
    measure: Last Balance
    per: [Account]
    aggregation: sum
    population: dimension
    carryForward: true
"""


@pytest.mark.parametrize(
    "where",
    [[], [{"field": "Snapshot Date", "op": ">=", "value": "2026-02-01"}]],
    ids=["unfiltered", "from-february"],
)
def test_a_carried_group_without_a_member_reads_past_the_date_filter(where: list[dict]) -> None:
    """The orphan's January balance of 100 is carried into February beside the
    account's 20. Its key was read from the filtered facts, which have no
    January, so the filtered February lost it: 20 instead of 120."""
    con = duckdb.connect()
    con.execute("CREATE TABLE accounts (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO accounts VALUES (1, 'A1')")
    con.execute(
        "CREATE TABLE snapshots (id INTEGER, account_id INTEGER, snap_date DATE, balance DOUBLE)"
    )
    con.execute(
        "INSERT INTO snapshots VALUES (1, NULL, DATE '2026-01-15', 100),"
        " (2, 1, DATE '2026-02-10', 20)"
    )
    raw = YAML(typ="safe").load(SNAPSHOT_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    query = QueryObject(
        **{
            "select": {"dimensions": ["Snapshot Date:month"], "measures": ["Closing Balance"]},
            "where": where,
        }
    )
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    by_month = {str(row[0])[:7]: float(row[1]) for row in con.execute(sql).fetchall()}
    assert by_month["2026-02"] == 120.0
