"""Measures anchored on different data objects, in one query.

An anchored measure runs on its anchor's own rows: ``Net`` once per customer,
``Net Per Order`` once per order. Asked together, the query rooted both at the
busier anchor in one star: ``Net``'s conformed subqueries joined on
``Customers`` before the star had joined it, and the database refused the SQL.
Each anchor now gets a union leg of its own.

Every customer here has orders and returns, so no conformed side is missing.
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
name: different_anchors
dataObjects:
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: customer_id, abstractType: string, primaryKey: true}
      Region: {code: region, abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: order_id, abstractType: string, primaryKey: true}
      Order Customer: {code: customer_id, abstractType: string}
      Order Amount: {code: amount, abstractType: int}
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
  Returns Less Orders:
    resultType: int
    aggregation: sum
    anchor: Returns
    expression: '{[Returns].[Return Amount]} - {[Orders].[Order Amount]}'
  Revenue: {columns: [{dataObject: Orders, column: Order Amount}], aggregation: sum}
metrics:
  Both: {expression: '{[Net]} + {[Net Per Order]}'}
"""

SETUP = """
CREATE TABLE customers AS SELECT * FROM (VALUES
  ('C1', 'EAST'), ('C2', 'EAST'), ('C3', 'WEST')) t(customer_id, region);
CREATE TABLE orders AS SELECT * FROM (VALUES
  ('O1', 'C1', 100), ('O2', 'C1', 200), ('O3', 'C2', 50), ('O4', 'C3', 300)
) t(order_id, customer_id, amount);
CREATE TABLE returns AS SELECT * FROM (VALUES
  ('R1', 'C1', 25), ('R2', 'C2', 10), ('R3', 'C3', 5), ('R4', 'C1', 15)
) t(return_id, customer_id, amount);
"""

# Per customer: C1 300 - 40, C2 50 - 10, C3 300 - 5.
# Per order: O1 100 - 40, O2 200 - 40, O3 50 - 10, O4 300 - 5.
# Per return: R1 25 - 300, R2 10 - 50, R3 5 - 300, R4 15 - 300.
EXPECTED: dict[str, tuple[int, dict[str, int]]] = {
    "Net": (595, {"EAST": 300, "WEST": 295}),
    "Net Per Order": (555, {"EAST": 260, "WEST": 295}),
    "Returns Less Orders": (-895, {"EAST": -600, "WEST": -295}),
    "Revenue": (650, {"EAST": 350, "WEST": 300}),
    "Both": (1150, {"EAST": 560, "WEST": 590}),
}


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


COMBINATIONS = [
    ["Net", "Net Per Order"],
    ["Net", "Net Per Order", "Revenue"],
    ["Net Per Order", "Returns Less Orders"],
    ["Net", "Returns Less Orders"],
    ["Both"],
    ["Both", "Revenue"],
]


@pytest.mark.parametrize("measures", COMBINATIONS, ids=lambda m: " + ".join(m))
def test_each_anchor_keeps_its_grain_in_the_total(
    model: SemanticModel, measures: list[str]
) -> None:
    rows = _rows(model, {"select": {"measures": measures}})
    assert list(rows[0]) == [EXPECTED[m][0] for m in measures]


@pytest.mark.parametrize("measures", COMBINATIONS, ids=lambda m: " + ".join(m))
def test_each_anchor_keeps_its_grain_per_group(model: SemanticModel, measures: list[str]) -> None:
    rows = _rows(model, {"select": {"dimensions": ["Region"], "measures": measures}})
    by_region = {row[0]: list(row[1:]) for row in rows}
    assert by_region == {
        region: [EXPECTED[m][1][region] for m in measures] for region in ("EAST", "WEST")
    }


def test_a_filter_and_an_order_apply_to_every_leg(model: SemanticModel) -> None:
    query = {
        "select": {"dimensions": ["Region"], "measures": ["Net", "Net Per Order"]},
        "where": [{"field": "Region", "op": "=", "value": "EAST"}],
    }
    assert _rows(model, query) == [("EAST", 300, 260)]
    query = {
        "select": {"dimensions": ["Region"], "measures": ["Net", "Net Per Order"]},
        "orderBy": [{"field": "Net", "direction": "desc"}],
        "limit": 1,
    }
    assert _rows(model, query) == [("EAST", 300, 260)]


def test_one_anchor_stays_a_single_plan(model: SemanticModel) -> None:
    # Two measures on one anchor share its grain, so nothing needs a union.
    query = QueryObject(select={"measures": ["Net Per Order", "Revenue"]})
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    assert "UNION ALL" not in sql.upper()
