"""A conformed fact with no rows for an anchor row adds nothing, not NULL.

``SUM(orders.amount - returns.amount)`` reads two facts that only meet at
``Customers``. Each is summed per customer and the expression evaluated once per
customer. A customer with orders and no returns has no returns sum; read as
NULL, it took the whole term with it and the total stopped adding up. The data
is the Apache Ossie wide-table compliance example (case e13), plus a customer
with no rows in either fact.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """\
version: 1.0
name: conformed_missing_side
dataObjects:
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: customer_id, abstractType: string, primaryKey: true}
      Region: {code: region, abstractType: string}
      Credit: {code: credit, abstractType: int}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: order_id, abstractType: string, primaryKey: true}
      Order Customer: {code: customer_id, abstractType: string}
      Order Amount: {code: amount, abstractType: int}
      Adjusted: {expression: '{Order Amount} + {[Customers].[Credit]}', abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: return_id, abstractType: string, primaryKey: true}
      Return Customer: {code: customer_id, abstractType: string}
      Return Amount: {code: amount, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer]
        columnsTo: [Customer ID]
dimensions:
  Region: {dataObject: Customers, column: Region, resultType: string}
measures:
  Net:
    resultType: int
    aggregation: sum
    expression: '{[Orders].[Order Amount]} - {[Returns].[Return Amount]}'
  Net Per Order:
    resultType: int
    aggregation: sum
    anchor: Orders
    expression: '{[Orders].[Order Amount]} - {[Returns].[Return Amount]}'
  Product:
    resultType: int
    aggregation: sum
    expression: '{[Orders].[Order Amount]} * {[Returns].[Return Amount]}'
  Net Credit:
    resultType: int
    aggregation: sum
    expression: '{[Orders].[Order Amount]} + {[Customers].[Credit]} - {[Returns].[Return Amount]}'
  Net Credit Computed:
    resultType: int
    aggregation: sum
    expression: '{[Orders].[Adjusted]} - {[Returns].[Return Amount]}'
  Average Net:
    resultType: float
    aggregation: avg
    expression: '{[Orders].[Order Amount]} - {[Returns].[Return Amount]}'
  Average Net Filtered:
    resultType: float
    aggregation: avg
    expression: '{[Orders].[Order Amount]} - {[Returns].[Return Amount]}'
    filters:
      - column: {dataObject: Customers, column: Region}
        operator: notequals
        values: [{dataType: string, valueString: NOWHERE}]
  Net Filtered:
    resultType: int
    aggregation: sum
    defaultValue: -1
    expression: '{[Orders].[Order Amount]} - {[Returns].[Return Amount]}'
    filters:
      - column: {dataObject: Customers, column: Region}
        operator: notequals
        values: [{dataType: string, valueString: NOWHERE}]
"""

SETUP = """
CREATE TABLE customers AS SELECT * FROM (VALUES
  ('C1', 'EAST', 1), ('C2', 'EAST', 2), ('C3', 'WEST', 3), ('C4', 'NORTH', 4),
  ('C5', 'SOUTH', 10)
) t(customer_id, region, credit);
CREATE TABLE orders AS SELECT * FROM (VALUES
  ('O1', 'C1', 100), ('O2', 'C1', 200), ('O3', 'C2', 50), ('O4', 'C3', 300)
) t(order_id, customer_id, amount);
CREATE TABLE returns AS SELECT * FROM (VALUES
  ('R1', 'C1', 25), ('R2', 'C4', 10)
) t(return_id, customer_id, amount);
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _rows(model: SemanticModel, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    con = duckdb.connect()
    con.execute(SETUP)
    return con.execute(sql).fetchall()


def test_the_total_adds_up(model: SemanticModel) -> None:
    # 650 ordered, 35 returned. Reading a missing side as NULL gave 275: only
    # C1 has both.
    assert _rows(model, {"select": {"measures": ["Net"]}}) == [(615,)]


def test_each_group_adds_up_and_a_group_without_fact_rows_stays_null(
    model: SemanticModel,
) -> None:
    rows = _rows(model, {"select": {"dimensions": ["Region"], "measures": ["Net"]}})
    # EAST: C1 300 - 25, C2 50 - 0. WEST: 300 - 0. NORTH: 0 - 10. SOUTH has
    # customers but no orders and no returns: a SUM over no rows, NULL.
    assert sorted(rows, key=lambda r: r[0]) == [
        ("EAST", 325),
        ("NORTH", -10),
        ("SOUTH", None),
        ("WEST", 300),
    ]


def test_an_anchor_fact_row_reads_a_missing_side_as_zero(model: SemanticModel) -> None:
    # Per order: O1 100 - 25, O2 200 - 25, O3 50 - 0, O4 300 - 0. The order row
    # exists, so its missing returns are 0 even though nothing else is read.
    assert _rows(model, {"select": {"measures": ["Net Per Order"]}}) == [(600,)]


def test_a_missing_side_in_a_product_contributes_nothing(model: SemanticModel) -> None:
    # Only C1 has both: 300 * 25. The others multiply by 0 and add nothing,
    # which the SUM shows the same way NULL did.
    assert _rows(model, {"select": {"measures": ["Product"]}}) == [(7500,)]


def test_a_filter_on_the_anchor_keeps_an_empty_customer_out_of_an_average(
    model: SemanticModel,
) -> None:
    # The filter reads Customers, the anchor, inside the aggregate. That made
    # C5 (no orders, no returns) a row of 0: 615 / 5 instead of 615 / 4.
    rows = _rows(model, {"select": {"measures": ["Average Net", "Average Net Filtered"]}})
    assert rows == [(pytest.approx(153.75), pytest.approx(153.75))]


def test_a_filter_on_the_anchor_keeps_an_empty_group_empty(model: SemanticModel) -> None:
    # SOUTH has no fact rows: NULL, so the measure's defaultValue applies.
    query = {"select": {"dimensions": ["Region"], "measures": ["Net Filtered"]}}
    assert dict(_rows(model, query)) == {"EAST": 325, "WEST": 300, "NORTH": -10, "SOUTH": -1}


def test_a_computed_column_reading_the_anchor_counts_as_reading_it(
    model: SemanticModel,
) -> None:
    # Credit is on Customers, the anchor, so every customer has a row: C5 adds
    # its credit of 10 although it has no orders and no returns. Through the
    # computed column Orders.Adjusted the measure names only the two facts, and
    # C5 read NULL. Per customer: 300 + 1 - 25, 50 + 2, 300 + 3, 4 - 10, 10.
    totals = _rows(model, {"select": {"measures": ["Net Credit", "Net Credit Computed"]}})
    assert totals == [(635, 635)]
    query = {
        "select": {"dimensions": ["Region"], "measures": ["Net Credit", "Net Credit Computed"]}
    }
    assert {row[0]: row[1:] for row in _rows(model, query)}["SOUTH"] == (10, 10)


@pytest.mark.parametrize(
    "dialect",
    ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "mysql", "postgres", "snowflake"],
)
def test_every_dialect_fills_inside_the_aggregate(model: SemanticModel, dialect: str) -> None:
    sql = (
        CompilationPipeline().compile(QueryObject(select={"measures": ["Net"]}), model, dialect).sql
    )
    # The fill sits on each conformed value, inside SUM, never around it.
    assert "COALESCE" in sql.upper()
    assert sql.upper().index("SUM(") < sql.upper().index("COALESCE")
