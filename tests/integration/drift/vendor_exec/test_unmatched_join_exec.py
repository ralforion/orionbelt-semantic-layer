"""An order whose customer is missing reads NULL for the customer's columns.

=========  ========        ===========  ========  ======
order      customer        customer     region    credit
=========  ========        ===========  ========  ======
``o1``     ``c1``          ``c1``       EU        10
``o2``     ``c9``          *(none)*
=========  ========        ===========  ========  ======

On ClickHouse the customer columns are not Nullable, and by default an
unmatched LEFT JOIN reads their type's default: ``o2`` was in region ``''``
with a credit of 0, and ``Region is null`` dropped it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

ORDERS = "unmatched_orders"
CUSTOMERS = "unmatched_customers"

MODEL_YAML = """
version: "1.0"
name: unmatched_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Customer Id: {{code: id, abstractType: string, primaryKey: true}}
      Region: {{code: region, abstractType: string}}
      Credit: {{code: credit, abstractType: float}}
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer Id]
dimensions:
  Order: {{dataObject: Orders, column: Order Id, resultType: string}}
  Region: {{dataObject: Customers, column: Region, resultType: string}}
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
    # ClickHouse's own non-Nullable String, where the default is ''.
    string = "String" if target.dialect == "clickhouse" else 0
    _create(
        target,
        ORDERS,
        [("id", 0), ("customer_id", 0), ("amount", 1)],
        [
            ("o1", "c1", 1),
            ("o2", "c9", 2),
        ],
    )
    _create(
        target, CUSTOMERS, [("id", string), ("region", string), ("credit", 1)], [("c1", "EU", 10)]
    )
    yaml_text = MODEL_YAML.format(
        orders=ORDERS, customers=CUSTOMERS, schema=SCHEMAS.get(target.dialect, "")
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _pairs(target: VendorTarget, model: SemanticModel, query: dict) -> dict[Any, Any]:
    dimension, measure = query["select"]["dimensions"][0], query["select"]["measures"][0]
    rows = _run(target, model, QueryObject(**query))
    return {row[dimension.lower()]: _number(row[measure.lower()]) for row in rows}


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    by_region = {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}
    assert _pairs(target, model, by_region) == {
        "EU": Decimal("1.00"),
        None: Decimal("2.00"),
    }, target.name
    by_order = {"select": {"dimensions": ["Order"], "measures": ["Total Credit"]}}
    assert _pairs(target, model, by_order) == {"o1": Decimal("10.00"), "o2": None}, target.name
    without_region = {**by_region, "where": [{"field": "Region", "op": "is_null"}]}
    assert _pairs(target, model, without_region) == {None: Decimal("2.00")}, target.name


def test_duckdb_unmatched_join(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_unmatched_join(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_unmatched_join(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_unmatched_join(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_unmatched_join(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_unmatched_join(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_unmatched_join(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_unmatched_join(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
