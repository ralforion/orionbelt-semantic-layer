"""A query returns its measures in the order it selects them.

A planner or wrapper projected a measure where it computed it: the union of
independent facts (CFL) each fact's measures together and the derived metrics
after them, the filter context pass the measures it scans apart last. Revenue
per order asked for first came back last, so a client reading the result by
position read another column, and a sort by position sorted by it.
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

MODEL = """\
version: 1.0
dataObjects:
  Orders:
    code: orders
    columns:
      Order ID: {code: order_id, abstractType: int}
      Order Customer: {code: customer_id, abstractType: int}
      Amount: {code: amount, abstractType: decimal}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Order Customer],
         columnsTo: [Customer ID]}
  Tickets:
    code: tickets
    columns:
      Ticket Customer: {code: customer_id, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Ticket Customer],
         columnsTo: [Customer ID]}
  Customers:
    code: customers
    columns:
      Customer ID: {code: customer_id, abstractType: int}
      Region: {code: region, abstractType: string}
dimensions:
  Region: {dataObject: Customers, column: Region, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum}
  Revenue Any Region:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    filterContext: {exclude: [Region]}
metrics:
  Revenue per Order: {expression: "{[Revenue]} / {[Orders Count]}"}
"""

# EU: two orders of 10 and 30, one ticket. US: one order of 5, three tickets.
DATA = (
    "CREATE TABLE customers AS SELECT * FROM (VALUES (1, 'EU'), (2, 'US')) t(customer_id, region);"
    "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 1, 10.0), (2, 1, 30.0), (3, 2, 5.0))"
    " t(order_id, customer_id, amount);"
    "CREATE TABLE tickets AS SELECT * FROM (VALUES (1), (2), (2), (2)) t(customer_id);"
)
EU = {"Revenue": 40, "Orders Count": 2, "Tickets Count": 1, "Revenue per Order": 20}
ANY_REGION = 45


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _run(model: SemanticModel, query: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
    sql = CompilationPipeline().compile(QueryObject.model_validate(query), model, "duckdb").sql
    con = duckdb.connect()
    con.execute(DATA)
    cursor = con.execute(sql)
    return [d[0] for d in cursor.description], cursor.fetchall()


@pytest.mark.parametrize(
    "measures",
    [
        ["Revenue", "Tickets Count", "Orders Count"],
        ["Revenue per Order", "Tickets Count", "Revenue"],
        ["Tickets Count", "Revenue per Order", "Orders Count"],
        ["Revenue Any Region", "Tickets Count", "Revenue"],
        ["Revenue Any Region", "Revenue", "Orders Count"],
    ],
    ids=[
        "a_fact_after_another_fact",
        "a_derived_metric_first",
        "a_derived_metric_between_facts",
        "a_filter_context_first_beside_another_fact",
        "a_filter_context_first",
    ],
)
def test_the_columns_come_in_the_select_order(model: SemanticModel, measures: list[str]) -> None:
    """The EU's: each a value of its own, the filter context's of every region."""
    names, rows = _run(
        model,
        {
            "select": {"measures": measures},
            "where": [{"field": "Region", "op": "=", "value": "EU"}],
        },
    )

    assert names == measures
    (row,) = rows
    values = {**EU, "Revenue Any Region": ANY_REGION}
    assert [float(v) for v in row] == [values[m] for m in measures]


def test_a_sort_by_position_sorts_by_the_column_selected_there(model: SemanticModel) -> None:
    """The third column is the tickets', which the US has more of: the union
    of the facts had projected the orders' count third."""
    names, rows = _run(
        model,
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Revenue", "Tickets Count", "Orders Count"],
            },
            "orderBy": [{"field": "3", "direction": "desc"}],
        },
    )

    assert names[2] == "Tickets Count"
    assert [r[0] for r in rows] == ["US", "EU"]
