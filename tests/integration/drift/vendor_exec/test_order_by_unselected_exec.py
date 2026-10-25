"""ORDER BY a measure or metric the query does not select, on every engine.

The measure is computed in a query of its own, LEFT JOINed onto the finished
rows by their dimensions (NULL-safe) and sorted on: a plain measure with LIMIT,
a cumulative metric, and a measure of a fact some rows have nothing in.
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

CUSTOMERS = "obu_customers"
ORDERS = "obu_orders"
RETURNS = "obu_returns"

CUSTOMER_ROWS = [("c1", "Ann"), ("c2", "Bob"), ("c3", "Cy")]
ORDER_ROWS = [
    ("o1", "c1", "2025-01-05", 10.0),
    ("o2", "c2", "2025-01-09", 50.0),
    ("o3", "c2", "2025-02-01", 5.0),
    ("o4", "c3", "2025-02-03", 30.0),
]
RETURN_ROWS = [("r1", "c2", 2), ("r2", "c3", 1)]

MODEL_YAML = """
version: "1.0"
name: order_by_unselected_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Cust ID: {{code: id, abstractType: string, primaryKey: true}}
      Name: {{code: name, abstractType: string}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Customer: {{code: customer_id, abstractType: string}}
      Order Date: {{code: order_date, abstractType: date}}
      Amount: {{code: amount, abstractType: float}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust ID]}}
  Returns:
    code: {returns}
    schema: '{schema}'
    columns:
      Return ID: {{code: id, abstractType: string, primaryKey: true}}
      Customer: {{code: customer_id, abstractType: string}}
      Quantity: {{code: quantity, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust ID]}}
dimensions:
  Customer Name: {{dataObject: Customers, column: Name, resultType: string}}
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Returned:
    columns: [{{dataObject: Returns, column: Quantity}}]
    aggregation: sum
    resultType: int
metrics:
  Running Revenue: {{type: cumulative, measure: Revenue, timeDimension: Order Month}}
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, CUSTOMERS, [("id", 0), ("name", 0)], CUSTOMER_ROWS)
    _create(
        target,
        ORDERS,
        [("id", 0), ("customer_id", 0), ("order_date", 3), ("amount", 1)],
        ORDER_ROWS,
    )
    _create(target, RETURNS, [("id", 0), ("customer_id", 0), ("quantity", 2)], RETURN_ROWS)
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


def _column(target: VendorTarget, model: SemanticModel, query: dict, name: str) -> list[str]:
    rows = _run(target, model, QueryObject(**query))
    # Only the selected columns come back.
    assert all(set(row) == {name} for row in rows), rows
    return [str(row[name])[:10] for row in rows]


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)

    top_two = {
        "select": {"dimensions": ["Customer Name"]},
        "orderBy": [{"field": "Revenue", "direction": "desc"}],
        "limit": 2,
    }
    assert _column(target, model, top_two, "customer name") == ["Bob", "Cy"]

    by_running = {
        "select": {"dimensions": ["Order Month"]},
        "orderBy": [{"field": "Running Revenue", "direction": "desc"}],
    }
    assert _column(target, model, by_running, "order month") == ["2025-02-01", "2025-01-01"]

    # Ann returned nothing; the explicit NULLS LAST keeps her last everywhere.
    by_returns = {
        "select": {"dimensions": ["Customer Name"]},
        "orderBy": [{"field": "Returned", "direction": "desc", "nulls": "last"}],
    }
    assert _column(target, model, by_returns, "customer name") == ["Bob", "Cy", "Ann"]


def test_duckdb_order_by_unselected(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_order_by_unselected(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_order_by_unselected(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_order_by_unselected(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_order_by_unselected(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_order_by_unselected(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_order_by_unselected(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_order_by_unselected(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
