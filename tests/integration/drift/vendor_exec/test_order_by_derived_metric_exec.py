"""ORDER BY a derived metric, executed on every engine.

The sort key is the metric's own column. Its formula over the components'
aliases named columns the query did not select, and the database refused it.
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

ORDERS = "derived_order_orders"

# Average order: c1 50, c2 300, c3 100.
ORDER_ROWS = [("o1", "c1", 40), ("o2", "c1", 60), ("o3", "c2", 300), ("o4", "c3", 100)]

MODEL_YAML = """
version: "1.0"
name: order_by_derived_metric_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Customer: {{code: customer_id, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
dimensions:
  Customer: {{dataObject: Orders, column: Customer, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
  Order Count:
    columns: [{{dataObject: Orders, column: Order ID}}]
    aggregation: count
metrics:
  Average Order:
    expression: '{{[Revenue]}} / {{[Order Count]}}'
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, ORDERS, [("id", 0), ("customer_id", 0), ("amount", 1)], ORDER_ROWS)
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
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
                "select": {"dimensions": ["Customer"], "measures": ["Average Order"]},
                "orderBy": [{"field": "Average Order", "direction": "desc"}],
            }
        ),
    )
    assert [row["customer"] for row in rows] == ["c2", "c3", "c1"]


def test_duckdb_order_by_derived_metric(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_order_by_derived_metric(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_order_by_derived_metric(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_order_by_derived_metric(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_order_by_derived_metric(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_order_by_derived_metric(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_order_by_derived_metric(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_order_by_derived_metric(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
