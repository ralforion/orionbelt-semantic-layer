"""A union leg whose fact cannot reach a dimension says so.

The leg NULL-pads the dimension, so every value of its measures lands in the
row where the dimension is NULL, and the other rows read 0 or NULL for it. That
answer adds up, but without a word it reads like a fact about each group. The
shape is the Apache Ossie wide-table compliance example (case e16): tickets
join nothing, revenue reaches customers.
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.warnings import WarningCode
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """\
version: 1.0
name: dimension_not_applied
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
      Amount: {code: amount, abstractType: int}
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
  Tickets:
    code: tickets
    schema: main
    columns:
      Ticket ID: {code: ticket_id, abstractType: string, primaryKey: true}
dimensions:
  Region: {dataObject: Customers, column: Region, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum}
  Returned: {columns: [{dataObject: Returns, column: Return Amount}], aggregation: sum}
"""

SETUP = """
CREATE TABLE customers AS SELECT * FROM (VALUES ('C1', 'EAST'), ('C2', 'WEST'))
  t(customer_id, region);
CREATE TABLE orders AS SELECT * FROM (VALUES ('O1', 'C1', 100), ('O2', 'C2', 300))
  t(order_id, customer_id, amount);
CREATE TABLE returns AS SELECT * FROM (VALUES ('R1', 'C1', 25))
  t(return_id, customer_id, amount);
CREATE TABLE tickets AS SELECT * FROM (VALUES ('T1'), ('T2'), ('T3')) t(ticket_id);
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _compile(
    model: SemanticModel, measures: list[str], dialect: str = "duckdb"
) -> CompilationResult:
    query = QueryObject(select={"dimensions": ["Region"], "measures": measures})
    return CompilationPipeline().compile(query, model, dialect)


def _codes(result: CompilationResult) -> list[str]:
    return [w.code for w in result.warnings]


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake"])
def test_a_measure_whose_fact_cannot_reach_the_dimension_is_reported(
    model: SemanticModel, dialect: str
) -> None:
    result = _compile(model, ["Revenue", "Tickets Count"], dialect)
    found = [w for w in result.warnings if w.code == WarningCode.DIMENSION_NOT_APPLIED]
    assert len(found) == 1
    assert found[0].context == {"measures": ["Tickets Count"], "dimensions": ["Region"]}
    assert "'Tickets Count' cannot be grouped by 'Region'" in found[0].message
    assert "where 'Region' is NULL" in found[0].message


def test_the_values_are_kept_in_the_null_row(model: SemanticModel) -> None:
    con = duckdb.connect()
    con.execute(SETUP)
    rows = con.execute(_compile(model, ["Revenue", "Tickets Count"]).sql).fetchall()
    # Every ticket is in the NULL row; the other rows count none of them.
    assert sorted(rows, key=lambda r: (r[0] is None, r[0])) == [
        ("EAST", 100, 0),
        ("WEST", 300, 0),
        (None, None, 3),
    ]


def test_facts_that_all_reach_the_dimension_are_not_reported(model: SemanticModel) -> None:
    assert WarningCode.DIMENSION_NOT_APPLIED not in _codes(_compile(model, ["Revenue", "Returned"]))


def test_a_single_fact_query_is_not_reported(model: SemanticModel) -> None:
    assert WarningCode.DIMENSION_NOT_APPLIED not in _codes(_compile(model, ["Revenue"]))


ROLES_YAML = """\
version: 1.0
name: role_dimensions
dataObjects:
  Customers:
    code: customers
    columns:
      Customer ID: {code: customer_id, abstractType: string, primaryKey: true}
      Region: {code: region, abstractType: string}
  Orders:
    code: orders
    columns:
      Order ID: {code: order_id, abstractType: string, primaryKey: true}
      Order Customer: {code: customer_id, abstractType: string}
      Amount: {code: amount, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
        pathName: customer
  Returns:
    code: returns
    columns:
      Return ID: {code: return_id, abstractType: string, primaryKey: true}
      Return Customer: {code: customer_id, abstractType: string}
      Return Amount: {code: amount, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer]
        columnsTo: [Customer ID]
        pathName: customer
dimensions:
  Order Region:
    {dataObject: Customers, column: Region, resultType: string, via: Orders, pathName: customer}
  Return Region:
    {dataObject: Customers, column: Region, resultType: string, via: Returns, pathName: customer}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum}
  Returned: {columns: [{dataObject: Returns, column: Return Amount}], aggregation: sum}
"""


def test_a_role_padded_by_the_other_fact_is_not_reported() -> None:
    # Each role belongs to one fact; the other leg pads it on purpose.
    raw, source_map = TrackedLoader().load_string(ROLES_YAML)
    roles, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject(
        select={
            "dimensions": ["Order Region", "Return Region"],
            "measures": ["Revenue", "Returned"],
        }
    )
    compiled = CompilationPipeline().compile(query, roles, "duckdb")
    assert WarningCode.DIMENSION_NOT_APPLIED not in _codes(compiled)
