"""Parents without children, and children without a parent, on every engine.

A customer measure beside an order measure is read through the orders; the
customers without orders are added as rows of their own, and an order without
a customer stays a row. The customers' CTE reads both and drives the query, so
both NULL groups have to survive each engine's union and outer join, and the
order's group has no customer value, not ClickHouse's default for one.
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
  Customer ID: {{dataObject: Customers, column: Customer ID, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
  Credit Or 99:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
    defaultValue: 99
  Big Order Credit:
    columns: [{{dataObject: Customers, column: Credit}}]
    aggregation: sum
    resultType: float
    defaultValue: 99
    filters:
      - column: {{dataObject: Orders, column: Amount}}
        operator: gt
        values: [{{dataType: float, valueFloat: 90}}]
metrics:
  Credit Score:
    expression: '{{[Total Credit]}} + 1'
  Defaulted Score:
    expression: '{{[Credit Or 99]}} + 1'
  Big Order Score:
    expression: '{{[Big Order Credit]}} + 1'
  Credit And Orders:
    expression: '{{[Total Credit]}} + {{[Orders Count]}}'
  Credit And Revenue:
    expression: '{{[Total Credit]}} + {{[Revenue]}}'
  Order Score:
    expression: '{{[Orders Count]}} + 1'
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

    # A metric's components are bare aggregates, not Nullable on ClickHouse:
    # the order without a customer read 0 there, so 1 and 1 instead of NULL
    # and 100 (the default, 99, plus one).
    rows = _run(
        target,
        _prepare(target),
        QueryObject(
            **{
                "select": {
                    "dimensions": ["Tier"],
                    "measures": ["Revenue", "Credit Score", "Defaulted Score"],
                }
            }
        ),
    )
    scores = {
        row["tier"]: (_number(row["credit score"]), _number(row["defaulted score"])) for row in rows
    }
    assert scores == {
        "gold": (Decimal(51), Decimal(51)),
        "silver": (Decimal(21), Decimal(21)),
        None: (None, Decimal(100)),
    }

    # Filtered on the orders, the credit has a CTE of its own, with no row for
    # a group none of whose orders passes: silver (70), and the order without
    # a customer. They read the default, 99, plus one; ClickHouse read 0 + 1.
    rows = _run(
        target,
        _prepare(target),
        QueryObject(
            **{"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Big Order Score"]}}
        ),
    )
    big = {row["tier"]: _number(row["big order score"]) for row in rows}
    assert big == {"gold": Decimal(11), "silver": Decimal(100), None: Decimal(100)}

    # The same under ROLLUP / CUBE, whose CTE stacks one aggregate per
    # grouping set. The grand total has a NULL tier too, so the tiers are read.
    # MySQL has no CUBE, and the dialect refuses it.
    groupings = ("rollup",) if target.dialect == "mysql" else ("rollup", "cube")
    for grouping in groupings:
        rows = _run(
            target,
            _prepare(target),
            QueryObject(
                **{
                    "select": {"dimensions": ["Tier"], "measures": ["Revenue", "Big Order Score"]},
                    "grouping": grouping,
                }
            ),
        )
        big = {row["tier"]: _number(row["big order score"]) for row in rows if row["tier"]}
        assert big == {"gold": Decimal(11), "silver": Decimal(100)}, grouping


def _assert_rolled_up(target: VendorTarget) -> None:
    """Each grouping set counts the customer without orders: gold's credit is
    50 with 2 customers, and the grand total 70 with 3, beside the revenue."""
    model = _prepare(target)
    for grouping in ("rollup",) if target.dialect == "mysql" else ("rollup", "cube"):
        rows = _run(
            target,
            model,
            QueryObject(
                **{
                    "select": {
                        "dimensions": ["Tier"],
                        "measures": ["Revenue", "Customers Count", "Total Credit"],
                    },
                    "grouping": grouping,
                }
            ),
        )
        by_set = {
            (row["tier"], int(row["_g_tier"])): (
                _number(row["revenue"]),
                _number(row["customers count"]),
                _number(row["total credit"]),
            )
            for row in rows
        }
        assert by_set == {
            ("gold", 0): (Decimal(150), Decimal(2), Decimal(50)),
            ("silver", 0): (Decimal(70), Decimal(1), Decimal(20)),
            (None, 0): (Decimal(5), Decimal(0), None),
            (None, 1): (Decimal(225), Decimal(3), Decimal(70)),
        }, grouping


def _assert_metrics_over_order_values(target: VendorTarget) -> None:
    """Grouped by the customer, the customer without orders is a group the
    order-side query has no row for. Her order count is 0 and her revenue
    NULL, before a metric is built over them: ClickHouse read the missing
    revenue as 0, so credit plus revenue gave 40 instead of NULL."""
    model = _prepare(target)
    for grouping in (None, "rollup"):
        query: dict = {
            "select": {
                "dimensions": ["Customer ID"],
                "measures": ["Credit And Orders", "Credit And Revenue", "Order Score"],
            }
        }
        if grouping:
            query["grouping"] = grouping
        rows = _run(target, model, QueryObject(**query))
        c3 = [row for row in rows if row["customer id"] == "c3"]
        assert [
            (
                _number(r["credit and orders"]),
                _number(r["credit and revenue"]),
                _number(r["order score"]),
            )
            for r in c3
        ] == [(Decimal(40), None, Decimal(1))], grouping


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


def test_duckdb_parents_without_children_rolled_up(vendor_duckdb: VendorTarget) -> None:
    _assert_rolled_up(vendor_duckdb)


def test_postgres_parents_without_children_rolled_up(vendor_postgres: VendorTarget) -> None:
    _assert_rolled_up(vendor_postgres)


def test_mysql_parents_without_children_rolled_up(vendor_mysql: VendorTarget) -> None:
    _assert_rolled_up(vendor_mysql)


def test_clickhouse_parents_without_children_rolled_up(vendor_clickhouse: VendorTarget) -> None:
    _assert_rolled_up(vendor_clickhouse)


def test_bigquery_parents_without_children_rolled_up(vendor_bigquery: VendorTarget) -> None:
    _assert_rolled_up(vendor_bigquery)


def test_snowflake_parents_without_children_rolled_up(vendor_snowflake: VendorTarget) -> None:
    _assert_rolled_up(vendor_snowflake)


def test_databricks_parents_without_children_rolled_up(vendor_databricks: VendorTarget) -> None:
    _assert_rolled_up(vendor_databricks)


def test_dremio_parents_without_children_rolled_up(vendor_dremio: VendorTarget) -> None:
    _assert_rolled_up(vendor_dremio)


def test_duckdb_metrics_over_order_values(vendor_duckdb: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_duckdb)


def test_postgres_metrics_over_order_values(vendor_postgres: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_postgres)


def test_mysql_metrics_over_order_values(vendor_mysql: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_mysql)


def test_clickhouse_metrics_over_order_values(vendor_clickhouse: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_clickhouse)


def test_bigquery_metrics_over_order_values(vendor_bigquery: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_bigquery)


def test_snowflake_metrics_over_order_values(vendor_snowflake: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_snowflake)


def test_databricks_metrics_over_order_values(vendor_databricks: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_databricks)


def test_dremio_metrics_over_order_values(vendor_dremio: VendorTarget) -> None:
    _assert_metrics_over_order_values(vendor_dremio)
