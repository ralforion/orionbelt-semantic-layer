"""``enforce: always`` holds on every scan a query plans, not only the main one.

Planning branches that scan rows of their own - the candidate sets of a
``dimensionsExclude`` query, the member list of a ``population: dimension``
metric - read past a filter applied only where the main query joins. Each
such scan applies an enforced filter or refuses the query.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
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
      Tenant ID: {code: tenant_id, abstractType: int}
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: int, primaryKey: true}
      Product Name: {code: name, abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Order Product ID: {code: product_id, abstractType: int}
      Order Tenant ID: {code: tenant_id, abstractType: int}
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
  Customer: {dataObject: Customers, column: Customer Name, resultType: string}
  Product: {dataObject: Products, column: Product Name, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    defaultValue: 0
metrics:
  Avg Revenue per Customer:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: avg
    population: dimension
"""


def _with_filter(data_object: str, column: str, enforce: str) -> str:
    return MODEL_YAML + (
        "filters:\n"
        f"  - {{dataObject: {data_object}, column: {column}, operator: equals, value: 1,"
        f" enforce: {enforce}}}\n"
    )


def _model(yaml_text: str) -> SemanticModel:
    raw = YAML(typ="safe").load(yaml_text)
    model, result = ReferenceResolver().resolve(raw)
    assert not result.errors, result.errors
    return model


def _sql(query: dict, yaml_text: str) -> str:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), "duckdb").sql


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, name VARCHAR, tenant_id INTEGER)")
    # Carol belongs to tenant 2.
    con.execute("INSERT INTO customers VALUES (1, 'Alice', 1), (2, 'Bob', 1), (3, 'Carol', 2)")
    con.execute("CREATE TABLE products (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO products VALUES (1, 'Pen'), (2, 'Ink')")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, product_id INTEGER,"
        " tenant_id INTEGER, amount DOUBLE)"
    )
    # Order 3 is Alice's, but booked under tenant 2.
    con.execute("INSERT INTO orders VALUES (1, 1, 1, 1, 10), (2, 3, 2, 2, 20), (3, 1, 2, 2, 30)")
    return con


EXCLUDE_QUERY = {"select": {"dimensions": ["Customer", "Product"]}, "dimensionsExclude": True}


@pytest.mark.parametrize(
    ("data_object", "column", "group"),
    [
        # Scoping the customer candidates is not enough: the product
        # candidates are read from Products, which never reaches Customers.
        # Unrefused, Carol (tenant 2) came back in two "missing" pairs.
        ("Customers", "Tenant ID", "'Product'"),
        # Neither candidate set reaches the tenant column on Orders.
        ("Orders", "Order Tenant ID", "'Customer'"),
    ],
)
def test_exclusion_candidates_that_cannot_reach_an_enforced_filter_refuse(
    data_object: str, column: str, group: str
) -> None:
    with pytest.raises(ResolutionError) as excinfo:
        _sql(EXCLUDE_QUERY, _with_filter(data_object, column, "always"))
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["STATIC_FILTER_UNREACHABLE"]
    assert f"candidate values of {group}" in errors[0].message


def test_exclusion_with_a_relevant_filter_is_unchanged(con: duckdb.DuckDBPyConnection) -> None:
    """Only ``always`` reaches the candidate scans; the default keeps the
    existing pairs filtered and the candidates whole."""
    sql = _sql(EXCLUDE_QUERY, _with_filter("Customers", "Tenant ID", "relevant"))
    assert ("Carol", "Ink") in con.execute(sql).fetchall()


def test_population_members_that_lose_an_enforced_filter_refuse() -> None:
    """The members are listed from Customers alone; the tenant filter on Orders
    cannot restrict them, so every tenant's customers would be counted."""
    with pytest.raises(ResolutionError) as excinfo:
        _sql(
            {"select": {"measures": ["Avg Revenue per Customer"]}},
            _with_filter("Orders", "Order Tenant ID", "always"),
        )
    assert "STATIC_FILTER_UNREACHABLE" in [e.code for e in excinfo.value.errors]


def test_population_members_apply_an_enforced_filter_on_their_object(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Alice 40 and Bob 0 (no orders) average to 20; Carol is tenant 2's."""
    sql = _sql(
        {"select": {"measures": ["Avg Revenue per Customer"]}},
        _with_filter("Customers", "Tenant ID", "always"),
    )
    assert [float(v) for (v,) in con.execute(sql).fetchall()] == [20.0]


def _exists_query(op: str) -> dict:
    return {
        "select": {"measures": ["Revenue"]},
        "where": [
            {
                "field": "Customers.Customer ID",
                "op": op,
                "subquery": {
                    "dataObject": "Orders",
                    "filter": [{"field": "Amount", "op": ">", "value": 15}],
                },
            }
        ],
    }


# Revenue declares defaultValue 0, so no matching rows reads 0.
@pytest.mark.parametrize(("op", "expected"), [("exists", [0.0]), ("nonexists", [10.0])])
def test_exists_body_applies_an_enforced_filter(
    con: duckdb.DuckDBPyConnection, op: str, expected: list[float]
) -> None:
    """Alice's only order over 15 is tenant 2's. Read past the tenant filter,
    the subquery answered from it: tenant 2's amount decided tenant 1's result."""
    sql = _sql(_exists_query(op), _with_filter("Orders", "Order Tenant ID", "always"))
    assert [float(v) for (v,) in con.execute(sql).fetchall()] == expected


def test_exists_body_needs_nothing_for_a_filter_on_the_subject(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """The body's rows belong to an outer customer the outer query filtered."""
    sql = _sql(_exists_query("exists"), _with_filter("Customers", "Tenant ID", "always"))
    assert [float(v) for (v,) in con.execute(sql).fetchall()] == [40.0]


def test_exists_body_with_a_relevant_filter_is_unchanged(con: duckdb.DuckDBPyConnection) -> None:
    sql = _sql(_exists_query("exists"), _with_filter("Orders", "Order Tenant ID", "relevant"))
    assert [float(v) for (v,) in con.execute(sql).fetchall()] == [10.0]


def test_exists_body_that_cannot_reach_an_enforced_filter_refuses() -> None:
    """The body reads Customers, which never reaches Products, and Products is
    not the correlation subject either."""
    query = {
        "select": {"measures": ["Revenue"]},
        "where": [
            {
                "field": "Orders.Order ID",
                "op": "exists",
                "subquery": {"dataObject": "Customers"},
            }
        ],
    }
    with pytest.raises(ResolutionError) as excinfo:
        _sql(query, _with_filter("Products", "Product ID", "always"))
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["STATIC_FILTER_UNREACHABLE"]
    assert "subquery on 'Customers'" in errors[0].message


WITH_RETURNS = MODEL_YAML.replace(
    "dimensions:\n",
    "  Returns:\n"
    "    code: returns\n"
    "    schema: main\n"
    "    columns:\n"
    "      Return ID: {code: id, abstractType: int, primaryKey: true}\n"
    "      Return Customer ID: {code: customer_id, abstractType: int}\n"
    "    joins:\n"
    "      - joinType: many-to-one\n"
    "        joinTo: Customers\n"
    "        columnsFrom: [Return Customer ID]\n"
    "        columnsTo: [Customer ID]\n"
    "dimensions:\n",
)
RAW_QUERY = {"select": {"fields": ["Orders.Order ID", "Returns.Return ID"]}}
TENANT_ON_ORDERS = (
    "filters:\n  - {dataObject: Orders, column: Order Tenant ID, operator: equals, value: 1"
)


def test_raw_leg_that_cannot_reach_an_enforced_filter_refuses() -> None:
    """Raw multi-fact mode lists each fact's rows in its own leg and skips a
    filter a leg cannot reach. An enforced one refuses instead."""
    with pytest.raises(ResolutionError) as excinfo:
        _sql(RAW_QUERY, WITH_RETURNS + TENANT_ON_ORDERS + ", enforce: always}\n")
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["STATIC_FILTER_UNREACHABLE"]
    assert "rows of 'Returns'" in errors[0].message


def test_raw_leg_that_reaches_a_relevant_filter_applies_it(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """The Orders leg lists tenant 1's order only; it lost the filter when the
    base object chosen for the query could not reach Orders. The Returns leg
    cannot apply it and says so."""
    con.execute("CREATE TABLE returns (id INTEGER, customer_id INTEGER)")
    con.execute("INSERT INTO returns VALUES (7, 1)")
    result = CompilationPipeline().compile(
        QueryObject(**RAW_QUERY), _model(WITH_RETURNS + TENANT_ON_ORDERS + "}\n"), "duckdb"
    )
    rows = sorted(con.execute(result.sql).fetchall(), key=str)
    assert rows == [(1, None), (None, 7)]
    assert [w.code for w in result.warnings] == ["STATIC_FILTER_NOT_APPLIED"]
