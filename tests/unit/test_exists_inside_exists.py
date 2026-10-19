"""``exists`` / ``nonexists`` inside a subquery's filters, executed on DuckDB.

An inner test correlates to the object its field is on, in the body around
it: the body joins that object when its path did not, then the inner body is
built like any other. It was refused (``NESTED_SUBQUERY_NOT_SUPPORTED``).

=========  ========  ======  ==============  ===========
customer   order     store   items           returned
=========  ========  ======  ==============  ===========
Ann        ``o1``    ``s1``  ``i1``          ``i1``
Bob        ``o2``    ``s2``  ``i2``
Bob        ``o3``    ``s1``  *(none)*
Cid        *(none)*
=========  ========  ======  ==============  ===========

Store ``s2`` is closed.
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.lineage import LineageBuilder, query_plan_facts

MODEL_YAML = """
version: "1.0"
name: exists_inside_exists
dataObjects:
  Customers:
    code: customers
    columns:
      Customer Id: {code: id, abstractType: string, primaryKey: true}
      Customer Name: {code: name, abstractType: string}
  Stores:
    code: stores
    columns:
      Store Id: {code: id, abstractType: string, primaryKey: true}
  Closures:
    code: closures
    columns:
      Closed Store Id: {code: store_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Stores
        columnsFrom: [Closed Store Id]
        columnsTo: [Store Id]
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Order Customer Id: {code: customer_id, abstractType: string}
      Order Store Id: {code: store_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer Id]
        columnsTo: [Customer Id]
      - joinType: many-to-one
        joinTo: Stores
        columnsFrom: [Order Store Id]
        columnsTo: [Store Id]
  Order Items:
    code: order_items
    columns:
      Item Id: {code: id, abstractType: string, primaryKey: true}
      Item Order Id: {code: order_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Item Order Id]
        columnsTo: [Order Id]
  Returns:
    code: returns
    columns:
      Returned Item Id: {code: item_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Order Items
        columnsFrom: [Returned Item Id]
        columnsTo: [Item Id]
dimensions:
  Customer Name: {dataObject: Customers, column: Customer Name, resultType: string}
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id VARCHAR, name VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('k1', 'Ann'), ('k2', 'Bob'), ('k3', 'Cid')")
    con.execute("CREATE TABLE stores (id VARCHAR)")
    con.execute("INSERT INTO stores VALUES ('s1'), ('s2')")
    con.execute("CREATE TABLE closures (store_id VARCHAR)")
    con.execute("INSERT INTO closures VALUES ('s2')")
    con.execute("CREATE TABLE orders (id VARCHAR, customer_id VARCHAR, store_id VARCHAR)")
    con.execute(
        "INSERT INTO orders VALUES ('o1', 'k1', 's1'), ('o2', 'k2', 's2'), ('o3', 'k2', 's1')"
    )
    con.execute("CREATE TABLE order_items (id VARCHAR, order_id VARCHAR)")
    con.execute("INSERT INTO order_items VALUES ('i1', 'o1'), ('i2', 'o2')")
    con.execute("CREATE TABLE returns (item_id VARCHAR)")
    con.execute("INSERT INTO returns VALUES ('i1')")
    return con


def _model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _exists(op: str, field: str, target: str, inner: list[dict] | None = None) -> dict:
    return {"field": field, "op": op, "subquery": {"dataObject": target, "filter": inner or []}}


def _query(where: dict) -> QueryObject:
    return QueryObject(
        **{
            "select": {"dimensions": ["Customer Name"]},
            "where": [where],
            "orderBy": [{"field": "Customer Name"}],
        }
    )


def _names(con: duckdb.DuckDBPyConnection, where: dict) -> list[str]:
    sql = CompilationPipeline().compile(_query(where), _model(), "duckdb").sql
    return [row[0] for row in con.execute(sql).fetchall()]


@pytest.mark.parametrize(
    ("op", "names"),
    [("exists", ["Ann"]), ("nonexists", ["Bob"])],
    ids=["returned-item", "kept-item"],
)
def test_three_levels(con: duckdb.DuckDBPyConnection, op: str, names: list[str]) -> None:
    """Customers with an order holding an item that was (or was not)
    returned: i1 was, i2 was not. Cid has no order, Bob's o3 no item."""
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [_exists("exists", "Order Id", "Order Items", [_exists(op, "Item Id", "Returns")])],
    )
    assert _names(con, where) == names


def test_an_inner_nonexists(con: duckdb.DuckDBPyConnection) -> None:
    """Customers with an order without items: Bob's o3."""
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [_exists("nonexists", "Order Id", "Order Items")],
    )
    assert _names(con, where) == ["Bob"]


def test_an_inner_subject_the_body_joins(con: duckdb.DuckDBPyConnection) -> None:
    """Customers with an order at a closed store: Bob's o2. The body joins
    the orders' stores to correlate the closures to them."""
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [_exists("exists", "Stores.Store Id", "Closures")],
    )
    assert _names(con, where) == ["Bob"]


def test_an_inner_body_reusing_an_outer_alias(con: duckdb.DuckDBPyConnection) -> None:
    """Customers with an order whose customer is Bob: Bob. The inner body
    reads the customers under the outer query's alias; its correlation is
    to the orders around it, and the outer correlation stays outside it."""
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [
            _exists(
                "exists",
                "Order Customer Id",
                "Customers",
                [{"field": "Customer Name", "op": "equals", "value": "Bob"}],
            )
        ],
    )
    assert _names(con, where) == ["Bob"]


def test_the_inner_tables_are_cache_dependencies() -> None:
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [_exists("exists", "Stores.Store Id", "Closures")],
    )
    compiled = CompilationPipeline().compile(_query(where), _model(), "duckdb")
    assert compiled.physical_tables == ["closures", "customers", "orders", "stores"]


def test_an_inner_subject_on_the_outer_subject_is_refused() -> None:
    """The outer subject is the correlation's other side: a test on it
    belongs in the outer ``where``."""
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [_exists("exists", "Customers.Customer Id", "Order Items")],
    )
    with pytest.raises(ResolutionError) as raised:
        CompilationPipeline().compile(_query(where), _model(), "duckdb")
    assert [e.code for e in raised.value.errors] == ["SUBQUERY_FILTER_OBJECT_NOT_JOINABLE"]


def test_lineage_follows_the_inner_subqueries() -> None:
    """The query reads the closures through the inner test, and the stores
    its field is on: neither was in the lineage graph."""
    where = _exists(
        "exists",
        "Customers.Customer Id",
        "Orders",
        [_exists("exists", "Stores.Store Id", "Closures")],
    )
    query, model = _query(where), _model()
    compiled = CompilationPipeline().compile(query, model, "duckdb")
    lineage = LineageBuilder(model).query(query, query_plan_facts(compiled))
    sources = {edge.source for edge in lineage.edges if edge.label == "exists"}
    assert {"data_object:Orders", "data_object:Closures"} <= sources
    assert any("Stores" in source for source in sources)
