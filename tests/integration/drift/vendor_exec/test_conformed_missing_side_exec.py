"""A conformed fact with no rows for an anchor row adds nothing, on every engine.

``SUM(orders.amount - returns.amount)`` over two facts that meet only at
customers: each is summed per customer, and a customer missing one side reads
that side as 0 (a sum over no rows), while a customer with neither stays NULL.
The data is the Apache Ossie wide-table compliance example (case e13), plus a
region whose customer has no rows in either fact.
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

CUSTOMERS = "conf_missing_customers"
ORDERS = "conf_missing_orders"
RETURNS = "conf_missing_returns"

CUSTOMER_ROWS = [("C1", "EAST"), ("C2", "EAST"), ("C3", "WEST"), ("C4", "NORTH"), ("C5", "SOUTH")]
ORDER_ROWS = [("O1", "C1", 100), ("O2", "C1", 200), ("O3", "C2", 50), ("O4", "C3", 300)]
RETURN_ROWS = [("R1", "C1", 25), ("R2", "C4", 10)]

MODEL_YAML = """
version: "1.0"
name: conformed_missing_side_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer ID: {{code: customer_id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: order_id, abstractType: string, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: string}}
      Order Amount: {{code: amount, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Returns:
    code: {returns}
    schema: '{schema}'
    columns:
      Return ID: {{code: return_id, abstractType: string, primaryKey: true}}
      Return Customer: {{code: customer_id, abstractType: string}}
      Return Amount: {{code: amount, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer]
        columnsTo: [Customer ID]
dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
measures:
  Net:
    resultType: int
    aggregation: sum
    expression: '{{[Orders].[Order Amount]}} - {{[Returns].[Return Amount]}}'
  Net Per Order:
    resultType: int
    aggregation: sum
    anchor: Orders
    expression: '{{[Orders].[Order Amount]}} - {{[Returns].[Return Amount]}}'
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, CUSTOMERS, [("customer_id", 0), ("region", 0)], CUSTOMER_ROWS)
    _create(target, ORDERS, [("order_id", 0), ("customer_id", 0), ("amount", 2)], ORDER_ROWS)
    _create(target, RETURNS, [("return_id", 0), ("customer_id", 0), ("amount", 2)], RETURN_ROWS)
    yaml_text = MODEL_YAML.format(
        customers=CUSTOMERS,
        orders=ORDERS,
        returns=RETURNS,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    # Each measure on its own: two anchors in one query is a separate case.
    net = _run(target, model, QueryObject(select={"measures": ["Net"]}))
    per_order = _run(target, model, QueryObject(select={"measures": ["Net Per Order"]}))
    # 650 ordered, 35 returned; per order 75 + 175 + 50 + 300.
    assert _number(net[0]["net"]) == Decimal(615)
    assert _number(per_order[0]["net per order"]) == Decimal(600)
    by_region = _run(
        target, model, QueryObject(select={"dimensions": ["Region"], "measures": ["Net"]})
    )
    got = {row["region"]: _number(row["net"]) for row in by_region}
    # SOUTH's customer has no orders and no returns: NULL, as a SUM over no rows.
    assert got == {
        "EAST": Decimal(325),
        "WEST": Decimal(300),
        "NORTH": Decimal(-10),
        "SOUTH": None,
    }


def test_duckdb_conformed_missing_side(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_conformed_missing_side(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_conformed_missing_side(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_conformed_missing_side(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_conformed_missing_side(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_conformed_missing_side(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_conformed_missing_side(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_conformed_missing_side(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
