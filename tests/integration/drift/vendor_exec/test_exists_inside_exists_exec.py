"""``exists`` / ``nonexists`` inside a subquery's filters, on every engine.

An inner test is a correlated subquery inside a correlated subquery: it
correlates to the body around it, not to the outer query.

=========  ========  ======  ==============  ===========
customer   order     store   items           returned
=========  ========  ======  ==============  ===========
Ann        ``o1``    ``s1``  ``i1``          ``i1``
Bob        ``o2``    ``s2``  ``i2``
Bob        ``o3``    ``s1``  *(none)*
Cid        *(none)*
=========  ========  ======  ==============  ===========
"""

from __future__ import annotations

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

CUSTOMERS = "nested_exists_customers"
ORDERS = "nested_exists_orders"
ITEMS = "nested_exists_items"
RETURNS = "nested_exists_returns"

MODEL_YAML = """
version: "1.0"
name: exists_inside_exists_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer Id: {{code: id, abstractType: string, primaryKey: true}}
      Customer Name: {{code: name, abstractType: string}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer Id: {{code: customer_id, abstractType: string}}
      Order Store Id: {{code: store_id, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer Id]
        columnsTo: [Customer Id]
  Order Items:
    code: {items}
    schema: '{schema}'
    columns:
      Item Id: {{code: id, abstractType: string, primaryKey: true}}
      Item Order Id: {{code: order_id, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Item Order Id]
        columnsTo: [Order Id]
  Returns:
    code: {returns}
    schema: '{schema}'
    columns:
      Returned Item Id: {{code: item_id, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Order Items
        columnsFrom: [Returned Item Id]
        columnsTo: [Item Id]
dimensions:
  Customer Name: {{dataObject: Customers, column: Customer Name, resultType: string}}
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target, CUSTOMERS, [("id", 0), ("name", 0)], [("k1", "Ann"), ("k2", "Bob"), ("k3", "Cid")]
    )
    _create(
        target,
        ORDERS,
        [("id", 0), ("customer_id", 0), ("store_id", 0)],
        [("o1", "k1", "s1"), ("o2", "k2", "s2"), ("o3", "k2", "s1")],
    )
    _create(target, ITEMS, [("id", 0), ("order_id", 0)], [("i1", "o1"), ("i2", "o2")])
    _create(target, RETURNS, [("item_id", 0)], [("i1",)])
    yaml_text = MODEL_YAML.format(
        customers=CUSTOMERS,
        orders=ORDERS,
        items=ITEMS,
        returns=RETURNS,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _exists(op: str, field: str, target: str, inner: list[dict] | None = None) -> dict:
    return {"field": field, "op": op, "subquery": {"dataObject": target, "filter": inner or []}}


def _names(target: VendorTarget, model: SemanticModel, inner: list[dict]) -> list[str]:
    query = QueryObject(
        **{
            "select": {"dimensions": ["Customer Name"]},
            "where": [_exists("exists", "Customers.Customer Id", "Orders", inner)],
            "orderBy": [{"field": "Customer Name"}],
        }
    )
    return [row["customer name"] for row in _run(target, model, query)]


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    # An order with an item that was returned (i1), or was not (i2).
    for op, names in [("exists", ["Ann"]), ("nonexists", ["Bob"])]:
        inner = [_exists("exists", "Order Id", "Order Items", [_exists(op, "Item Id", "Returns")])]
        assert _names(target, model, inner) == names, f"{target.name} {op}"
    # An order without items: o3.
    inner = [_exists("nonexists", "Order Id", "Order Items")]
    assert _names(target, model, inner) == ["Bob"], target.name
    # An order whose customer is Bob: the inner body reuses the outer alias.
    bob = [{"field": "Customer Name", "op": "equals", "value": "Bob"}]
    inner = [_exists("exists", "Order Customer Id", "Customers", bob)]
    assert _names(target, model, inner) == ["Bob"], target.name


def test_duckdb_exists_inside_exists(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_exists_inside_exists(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_exists_inside_exists(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_exists_inside_exists(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_exists_inside_exists(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_exists_inside_exists(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_exists_inside_exists(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_exists_inside_exists(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
