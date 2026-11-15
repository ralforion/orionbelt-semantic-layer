"""Measures anchored on different data objects, in one query, on every engine.

``Net`` runs once per customer and ``Net Per Order`` once per order. Asked
together they compiled to one star rooted at orders, joining ``Net``'s conformed
facts on a customers table it had not joined yet. Each anchor now gets a union
leg of its own. Every customer has orders and returns, so no side is missing.
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

CUSTOMERS = "two_anchor_customers"
ORDERS = "two_anchor_orders"
RETURNS = "two_anchor_returns"

CUSTOMER_ROWS = [("C1", "EAST"), ("C2", "EAST"), ("C3", "WEST")]
ORDER_ROWS = [("O1", "C1", 100), ("O2", "C1", 200), ("O3", "C2", 50), ("O4", "C3", 300)]
RETURN_ROWS = [("R1", "C1", 25), ("R2", "C2", 10), ("R3", "C3", 5), ("R4", "C1", 15)]

MODEL_YAML = """
version: "1.0"
name: different_anchors_vendor
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
  Revenue:
    columns: [{{dataObject: Orders, column: Order Amount}}]
    aggregation: sum
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
    measures = ["Net", "Net Per Order", "Revenue"]
    total = _run(target, model, QueryObject(select={"measures": measures}))
    assert [_number(total[0][m.lower()]) for m in measures] == [
        Decimal(595),
        Decimal(555),
        Decimal(650),
    ]
    rows = _run(target, model, QueryObject(select={"dimensions": ["Region"], "measures": measures}))
    got = {row["region"]: [_number(row[m.lower()]) for m in measures] for row in rows}
    assert got == {
        "EAST": [Decimal(300), Decimal(260), Decimal(350)],
        "WEST": [Decimal(295), Decimal(295), Decimal(300)],
    }


def test_duckdb_different_anchors(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_different_anchors(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_different_anchors(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_different_anchors(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_different_anchors(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_different_anchors(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_different_anchors(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_different_anchors(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
