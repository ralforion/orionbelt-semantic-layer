"""Parents without children, and children without a parent, on every engine.

A customer measure beside an order measure is read through the orders; the
customers without orders are added as rows of their own, and an order without
a customer stays a row. The customers' CTE reads both and drives the query, so
both NULL groups have to survive each engine's union and outer join.
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

CUSTOMERS = "pwc_customers"
ORDERS = "pwc_orders"

# c3 has no orders; o4 has no customer.
CUSTOMER_ROWS = [("c1", "gold", 10.0), ("c2", "silver", 20.0), ("c3", "gold", 40.0)]
ORDER_ROWS = [("o1", "c1", 100.0), ("o2", "c1", 50.0), ("o3", "c2", 70.0), ("o4", None, 5.0)]

MODEL_YAML = """
version: "1.0"
name: parents_without_children_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer ID: {{code: id, abstractType: string, primaryKey: true}}
      Tier: {{code: tier, abstractType: string}}
      Credit: {{code: credit, abstractType: float}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer ID: {{code: customer_id, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Tier: {{dataObject: Customers, column: Tier, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, CUSTOMERS, [("id", 0), ("tier", 0), ("credit", 1)], CUSTOMER_ROWS)
    _create(target, ORDERS, [("id", 0), ("customer_id", 0), ("amount", 1)], ORDER_ROWS)
    yaml_text = MODEL_YAML.format(
        customers=CUSTOMERS, orders=ORDERS, schema=SCHEMAS.get(target.dialect, "")
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _assert_all(target: VendorTarget) -> None:
    rows = _run(
        target,
        _prepare(target),
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Tier"],
                    "measures": ["Revenue", "Customers Count", "Total Credit"],
                }
            }
        ),
    )
    by_tier = {
        row["tier"]: (
            _number(row["revenue"]),
            _number(row["customers count"]),
            _number(row["total credit"]),
        )
        for row in rows
    }
    assert by_tier == {
        "gold": (Decimal(150), Decimal(2), Decimal(50)),
        "silver": (Decimal(70), Decimal(1), Decimal(20)),
        None: (Decimal(5), Decimal(0), None),
    }


def test_duckdb_parents_without_children(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_parents_without_children(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_parents_without_children(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_parents_without_children(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_parents_without_children(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_parents_without_children(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_parents_without_children(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_parents_without_children(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
