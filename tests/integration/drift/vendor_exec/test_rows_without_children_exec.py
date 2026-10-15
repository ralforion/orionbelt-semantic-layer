"""Parents without child rows that a many-side WHERE accepts, on every engine.

Customer measures with a WHERE on an Orders column are answered from the order
rows, with the customers without orders that the WHERE accepts added in a leg
of their own: a ``NOT EXISTS`` over the orders, the WHERE replayed with every
order column a typed NULL, unioned with the deduplicated rows. c3 (gold) and c4
have no orders, and South has only them, so the South group exists only
through that leg.
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

CUSTOMERS = "childless_customers"
ORDERS = "childless_orders"

CUSTOMER_ROWS = [
    ("c1", "North", "gold", 100),
    ("c2", "North", "silver", 200),
    ("c3", "South", "gold", 600),
    ("c4", "South", "bronze", 50),
]
ORDER_ROWS = [("o1", "c1", "ok", 10), ("o2", "c2", "bad", 20)]

MODEL_YAML = """
version: "1.0"
name: rows_without_children_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer ID: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Tier: {{code: tier, abstractType: string}}
      Credit: {{code: credit, abstractType: float}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order ID: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: string}}
      Status: {{code: status, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
      Status Or Missing: {{expression: "COALESCE({{Status}}, 'missing')", abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
  Status: {{dataObject: Orders, column: Status, resultType: string}}
  Status Or Missing: {{dataObject: Orders, column: Status Or Missing, resultType: string}}
measures:
  Customer Count:
    columns: [{{dataObject: Customers, column: Customer ID}}]
    aggregation: count
  Total Credit:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
  Order Count:
    columns: [{{dataObject: Orders, column: Order ID}}]
    aggregation: count
"""

GOLD_OR_OK = [
    {
        "logic": "or",
        "filters": [
            {"field": "Customers.Tier", "op": "=", "value": "gold"},
            {"field": "Status", "op": "=", "value": "ok"},
        ],
    }
]


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target,
        CUSTOMERS,
        [("id", 0), ("region", 0), ("tier", 0), ("credit", 1)],
        CUSTOMER_ROWS,
    )
    _create(
        target, ORDERS, [("id", 0), ("customer_id", 0), ("status", 0), ("amount", 1)], ORDER_ROWS
    )
    yaml_text = MODEL_YAML.format(
        customers=CUSTOMERS, orders=ORDERS, schema=SCHEMAS.get(target.dialect, "")
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
                    "dimensions": ["Region"],
                    "measures": ["Customer Count", "Total Credit", "Revenue", "Order Count"],
                },
                "where": GOLD_OR_OK,
            }
        ),
    )
    by_region = {
        row["region"]: (
            _number(row["customer count"]),
            _number(row["total credit"]),
            _number(row["revenue"]),
            _number(row["order count"]),
        )
        for row in rows
    }
    assert by_region == {
        "North": (Decimal(1), Decimal(100), Decimal(10), Decimal(1)),
        "South": (Decimal(1), Decimal(600), None, Decimal(0)),
    }

    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {"measures": ["Customer Count", "Total Credit"]},
                "where": [{"field": "Orders.Order ID", "op": "notset"}],
            }
        ),
    )
    assert [(_number(r["customer count"]), _number(r["total credit"])) for r in rows] == [
        (Decimal(2), Decimal(650))
    ]

    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {"dimensions": ["Status"], "measures": ["Customer Count"]},
                "where": GOLD_OR_OK,
            }
        ),
    )
    assert {row["status"]: _number(row["customer count"]) for row in rows} == {
        "ok": Decimal(1),
        None: Decimal(1),
    }

    # A computed child dimension reads its expression with the order columns
    # NULL: the customer without orders is 'missing', not NULL.
    rows = _run(
        target,
        model,
        QueryObject(
            **{
                "select": {"dimensions": ["Status Or Missing"], "measures": ["Customer Count"]},
                "where": GOLD_OR_OK,
            }
        ),
    )
    assert {row["status or missing"]: _number(row["customer count"]) for row in rows} == {
        "ok": Decimal(1),
        "missing": Decimal(1),
    }


def test_duckdb_rows_without_children(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_rows_without_children(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_rows_without_children(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_rows_without_children(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_rows_without_children(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_rows_without_children(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_rows_without_children(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_rows_without_children(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
