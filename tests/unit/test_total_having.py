"""A HAVING filter does not change a total or grain-override measure.

``total`` and ``grain: {mode: FIXED}`` sum every group at their grain in a
window over the grouped rows. A HAVING on another measure was evaluated in the
grouped query, before the window, so the parent total shrank to the groups it
kept: a region total of 695 where the region's cities add up to 890.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: int, primaryKey: true}
      Region: {code: region, abstractType: string}
      City: {code: city, abstractType: string}
      Amount: {code: amount, abstractType: float}
      Upper City: {expression: "UPPER({City})", abstractType: string}
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  City: {dataObject: Sales, column: City, resultType: string}
  Upper City: {dataObject: Sales, column: Upper City, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    resultType: float
  Region Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    resultType: float
    grain: {mode: FIXED, include: [Region]}
  Grand Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    resultType: float
    total: true
metrics:
  City Rank:
    type: window
    measure: Revenue
    windowFunction: rank
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE sales (id INTEGER, region VARCHAR, city VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO sales VALUES (1, 'South', 'Rome', 300), (2, 'South', 'Oslo', 195),"
        " (3, 'North', 'Bergen', 400), (4, 'North', 'Oslo', 100)"
    )
    return con


def _rows(con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple[object, ...]]:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return sorted(
        (row[0], row[1], *(float(v) for v in row[2:])) for row in con.execute(sql).fetchall()
    )


@pytest.mark.parametrize(
    ("measure", "south", "north"),
    [("Region Total", 495.0, 500.0), ("Grand Total", 995.0, 995.0)],
)
def test_having_on_another_measure_keeps_the_total(
    con: duckdb.DuckDBPyConnection, measure: str, south: float, north: float
) -> None:
    rows = _rows(
        con,
        {
            "select": {"dimensions": ["Region", "City"], "measures": ["Revenue", measure]},
            "having": [{"field": "Revenue", "op": "gt", "value": 250}],
        },
    )
    assert rows == [("North", "Bergen", 400.0, north), ("South", "Rome", 300.0, south)]


@pytest.mark.parametrize("measure", ["Grand Total", "City Rank"])
def test_having_on_a_computed_dimension_past_a_window(
    con: duckdb.DuckDBPyConnection, measure: str
) -> None:
    """The predicate tests UPPER(city) as a whole. Past the grouped query only
    the dimension's alias is in scope; rewriting just its column left
    ``"Sales"."city"`` behind and the query was refused."""
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    query = {
        "select": {"dimensions": ["Upper City"], "measures": ["Revenue", measure]},
        "having": [{"field": "Upper City", "op": "like", "value": "%O%"}],
    }
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    assert sorted(row[0] for row in con.execute(sql).fetchall()) == ["OSLO", "ROME"]
