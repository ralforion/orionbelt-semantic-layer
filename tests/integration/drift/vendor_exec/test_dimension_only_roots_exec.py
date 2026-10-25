"""A dimension-only query over several roots, on every engine.

Orders and Returns both join Customers and Products: region by category is
the union of the pairs each fact holds, and ``dimensionsExclude`` takes away
the pairs of both. A dimension computed from another object's column
(the customer's zone) is joined in each leg.
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

CUSTOMERS = "dor_customers"
PRODUCTS = "dor_products"
ORDERS = "dor_orders"
RETURNS = "dor_returns"
ZONES = "dor_zones"

# o3's customer is unknown, so its pair has no region.
CUSTOMER_ROWS = [("k1", "North", "z1"), ("k2", "South", "z1"), ("k3", "East", "z2")]
ZONE_ROWS = [("z1", "inland"), ("z2", "coast")]
PRODUCT_ROWS = [("p1", "Books"), ("p2", "Games")]
ORDER_ROWS = [("o1", "k1", "p1"), ("o2", "k2", "p2"), ("o3", "k9", "p1")]
RETURN_ROWS = [("r1", "k2", "p1"), ("r2", "k3", "p2")]

MODEL_YAML = """
version: "1.0"
name: dimension_only_roots_vendor
dataObjects:
  Zones:
    code: {zones}
    schema: '{schema}'
    columns:
      Zone ID: {{code: id, abstractType: string, primaryKey: true}}
      Zone Name: {{code: name, abstractType: string}}
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Cust ID: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Zone: {{code: zone_id, abstractType: string}}
      Zone Upper: {{expression: "upper({{[Zones].[Zone Name]}})", abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Zones, columnsFrom: [Zone], columnsTo: [Zone ID]}}
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
      Customer: {{code: customer_id, abstractType: string}}
      Product: {{code: product_id, abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust ID]}}
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product ID]}}
  Returns:
    code: {returns}
    schema: '{schema}'
    columns:
      Return ID: {{code: id, abstractType: string, primaryKey: true}}
      Customer: {{code: customer_id, abstractType: string}}
      Product: {{code: product_id, abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust ID]}}
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product ID]}}
dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
  Category: {{dataObject: Products, column: Category, resultType: string}}
  Zone Upper: {{dataObject: Customers, column: Zone Upper, resultType: string}}
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, ZONES, [("id", 0), ("name", 0)], ZONE_ROWS)
    _create(target, CUSTOMERS, [("id", 0), ("region", 0), ("zone_id", 0)], CUSTOMER_ROWS)
    _create(target, PRODUCTS, [("id", 0), ("category", 0)], PRODUCT_ROWS)
    fact_columns = [("id", 0), ("customer_id", 0), ("product_id", 0)]
    _create(target, ORDERS, fact_columns, ORDER_ROWS)
    _create(target, RETURNS, fact_columns, RETURN_ROWS)
    yaml_text = MODEL_YAML.format(
        customers=CUSTOMERS,
        products=PRODUCTS,
        orders=ORDERS,
        returns=RETURNS,
        zones=ZONES,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _pairs(target: VendorTarget, model: SemanticModel, **query: object) -> set[tuple]:
    select = {"select": {"dimensions": ["Region", "Category"]}}
    rows = _run(target, model, QueryObject(**select, **query))
    return {(row["region"], row["category"]) for row in rows}


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    assert _pairs(target, model) == {
        ("North", "Books"),
        ("South", "Games"),
        (None, "Books"),
        ("South", "Books"),
        ("East", "Games"),
    }
    assert _pairs(target, model, dimensionsExclude=True) == {
        ("East", "Books"),
        ("North", "Games"),
    }
    assert _pairs(target, model, where=[{"field": "Region", "op": "=", "value": "South"}]) == {
        ("South", "Books"),
        ("South", "Games"),
    }

    # The zone is read through Customers in each leg; o3's customer is unknown.
    rows = _run(
        target, model, QueryObject(**{"select": {"dimensions": ["Zone Upper", "Category"]}})
    )
    assert {(row["zone upper"], row["category"]) for row in rows} == {
        ("INLAND", "Books"),
        ("INLAND", "Games"),
        ("COAST", "Games"),
        (None, "Books"),
    }
    measures = ["Orders Count", "Returns Count"]
    rows = _run(
        target,
        model,
        QueryObject(**{"select": {"dimensions": ["Zone Upper"], "measures": measures}}),
    )
    assert {
        row["zone upper"]: tuple(_number(row[name.lower()]) for name in measures) for row in rows
    } == {
        "INLAND": (Decimal(2), Decimal(1)),
        "COAST": (Decimal(0), Decimal(1)),
        None: (Decimal(1), Decimal(0)),
    }


def test_duckdb_dimension_only_roots(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_dimension_only_roots(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_dimension_only_roots(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_dimension_only_roots(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_dimension_only_roots(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_dimension_only_roots(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_dimension_only_roots(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_dimension_only_roots(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
