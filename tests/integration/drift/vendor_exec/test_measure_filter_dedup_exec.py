"""A measure filter on another object, applied before deduplicating, on every engine.

A customer's credit read through Orders repeats per order and is deduplicated
per customer. Its filter reads Products, two joins away, so it is the ``WHERE``
of the deduplicating ``SELECT``: in the ``DISTINCT`` it would keep a row per
order. A measure without the filter, beside it, is not narrowed. A filter only
customers without orders pass adds them as rows of their own, and a group the
filter empties reads the measure's default.
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

CUSTOMERS = "mf_dedup_customers"
PRODUCTS = "mf_dedup_products"
ORDERS = "mf_dedup_orders"

# c3 has no orders.
CUSTOMER_ROWS = [("c1", "gold", 10.0), ("c2", "silver", 20.0), ("c3", "gold", 40.0)]
PRODUCT_ROWS = [("p1", "tools"), ("p2", "toys")]
# c1 buys tools twice, c2 toys once.
ORDER_ROWS = [("o1", "c1", "p1"), ("o2", "c1", "p1"), ("o3", "c2", "p2")]

MODEL_YAML = """
version: "1.0"
name: measure_filter_dedup_vendor
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
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer ID: {{code: customer_id, abstractType: string}}
      Order Product ID: {{code: product_id, abstractType: string}}
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
measures:
  Credit Buying Tools:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
    filters:
      - column: {{dataObject: Products, column: Category}}
        operator: equals
        values: [{{dataType: string, valueString: tools}}]
  Credit With Orders:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
    filters:
      - column: {{dataObject: Orders, column: Order ID}}
        operator: set
  Credit Without Orders:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
    filters:
      - column: {{dataObject: Orders, column: Order ID}}
        operator: notset
  Tools Credit Or Zero:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
    defaultValue: 0
    filters:
      - column: {{dataObject: Products, column: Category}}
        operator: equals
        values: [{{dataType: string, valueString: tools}}]
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, CUSTOMERS, [("id", 0), ("tier", 0), ("credit", 1)], CUSTOMER_ROWS)
    _create(target, PRODUCTS, [("id", 0), ("category", 0)], PRODUCT_ROWS)
    _create(target, ORDERS, [("id", 0), ("customer_id", 0), ("product_id", 0)], ORDER_ROWS)
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
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Tier"],
                    "measures": ["Credit Buying Tools", "Credit With Orders"],
                }
            }
        ),
    )
    by_tier = {
        row["tier"]: (_number(row["credit buying tools"]), _number(row["credit with orders"]))
        for row in rows
    }
    # gold: c1 once (10) for both; c3 has no orders. silver: c2 bought toys.
    assert by_tier == {"gold": (Decimal(10), Decimal(10)), "silver": (None, Decimal(20))}

    # c3 is in no order row: added as a parent without children, for which
    # "no order" holds. Silver bought no tools and reads the default.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Tier"],
                    "measures": ["Credit Without Orders", "Tools Credit Or Zero"],
                }
            }
        ),
    )
    by_tier = {
        row["tier"]: (_number(row["credit without orders"]), _number(row["tools credit or zero"]))
        for row in rows
    }
    assert by_tier == {"gold": (Decimal(40), Decimal(10)), "silver": (None, Decimal(0))}


def test_duckdb_measure_filter_dedup(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_measure_filter_dedup(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_measure_filter_dedup(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_measure_filter_dedup(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_measure_filter_dedup(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_measure_filter_dedup(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_measure_filter_dedup(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_measure_filter_dedup(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
