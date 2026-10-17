"""Several parents' rows without children, on every engine.

A customer measure and a product measure beside an order measure: the
customers without orders and the products never ordered are each added as a
set of their own, and the groups are listed from every CTE.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _number, _run

pytestmark = pytest.mark.docker

CUSTOMERS = "spw_customers"
PRODUCTS = "spw_products"
ORDERS = "spw_orders"

# c3 has no orders, p3 is never ordered, o4 has no customer.
CUSTOMER_ROWS = [("c1", "gold", 10.0), ("c2", "silver", 20.0), ("c3", "gold", 40.0)]
PRODUCT_ROWS = [("p1", "tools", 3.0), ("p2", "toys", 4.0), ("p3", "games", 8.0)]
ORDER_ROWS = [
    ("o1", "c1", "p1", 100.0),
    ("o2", "c1", "p1", 50.0),
    ("o3", "c2", "p2", 70.0),
    ("o4", None, "p1", 5.0),
]

MODEL_YAML = """
version: "1.0"
name: several_parents_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer ID: {{code: id, abstractType: string, primaryKey: true}}
      Tier: {{code: tier, abstractType: string}}
      Credit: {{code: credit, abstractType: float}}
  Products:
    code: {products}
    schema: '{schema}'
    columns:
      Product ID: {{code: id, abstractType: string, primaryKey: true}}
      Category: {{code: category, abstractType: string}}
      Price: {{code: price, abstractType: float}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer ID: {{code: customer_id, abstractType: string}}
      Order Product ID: {{code: product_id, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
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
  Tier: {{dataObject: Customers, column: Tier, resultType: string}}
  Category: {{dataObject: Products, column: Category, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
  Total Price:
    columns: [{{dataObject: Products, column: Price}}]
    aggregation: sum
    resultType: float
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, CUSTOMERS, [("id", 0), ("tier", 0), ("credit", 1)], CUSTOMER_ROWS)
    _create(target, PRODUCTS, [("id", 0), ("category", 0), ("price", 1)], PRODUCT_ROWS)
    _create(
        target,
        ORDERS,
        [("id", 0), ("customer_id", 0), ("product_id", 0), ("amount", 1)],
        ORDER_ROWS,
    )
    yaml_text = MODEL_YAML.format(
        customers=CUSTOMERS,
        products=PRODUCTS,
        orders=ORDERS,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    measures = ["Revenue", "Total Credit", "Total Price"]
    rows = _run(
        target, model, QueryObject(**{"select": {"dimensions": ["Tier"], "measures": measures}})
    )
    by_tier = {row["tier"]: tuple(_number(row[name.lower()]) for name in measures) for row in rows}
    assert by_tier == {
        "gold": (Decimal(150), Decimal(50), Decimal(3)),
        "silver": (Decimal(70), Decimal(20), Decimal(4)),
        None: (Decimal(5), None, Decimal(11)),
    }

    # Carol (gold, no category) and games (no tier) both pass the OR.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {"measures": measures},
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Category", "op": "=", "value": "games"},
                            {"field": "Tier", "op": "=", "value": "gold"},
                        ],
                    }
                ],
            }
        ),
    )
    assert [tuple(_number(row[name.lower()]) for name in measures) for row in rows] == [
        (Decimal(150), Decimal(50), Decimal(11))
    ]


def test_duckdb_several_parents(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_several_parents(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_several_parents(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_several_parents(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_several_parents(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_several_parents(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_several_parents(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_several_parents(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
