"""A filter context's measure under a rollup or cube has its subtotals.

Its scan was grouped by the dimensions alone and joined to the query's rows
by them: a subtotal's NULL met no group, or a NULL member's, so the subtotals
and the grand total read NULL. And the result lost the GROUPING() flags that
tell a subtotal from a NULL member, beside a total too.
``test_rollup_filter_context_exec`` runs it on every engine.
"""

from __future__ import annotations

from decimal import Decimal
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
  Sales:
    code: sales
    columns:
      Region: {code: region, abstractType: string}
      City: {code: city, abstractType: string}
      Amount: {code: amount, abstractType: int}
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  City: {dataObject: Sales, column: City, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum}
  Revenue Any Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    filterContext: {exclude: [Region]}
  Revenue All:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    filterContext: {mode: FIXED}
  Revenue Total: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum, total: true}
"""

# n: 90 in cities a and b; s: 40; a NULL region the query's filter leaves out.
DATA = (
    "CREATE TABLE sales AS SELECT * FROM (VALUES ('n', 'a', 10), ('n', 'b', 20),"
    " ('s', 'c', 40), ('n', 'b', 60), (NULL, 'd', 5), (NULL, 'd', 7)) t(region, city, amount)"
)
IN_N_AND_S = [{"field": "Region", "op": "in", "value": ["n", "s"]}]


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _rows(model: SemanticModel, query: dict[str, Any]) -> list[dict[str, Any]]:
    sql = CompilationPipeline().compile(QueryObject.model_validate(query), model, "duckdb").sql
    con = duckdb.connect()
    con.execute(DATA)
    cursor = con.execute(sql)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


@pytest.mark.parametrize("grouping", ["rollup", "cube"])
@pytest.mark.parametrize("measure", ["Revenue Any Region", "Revenue All"])
def test_each_subtotal_is_the_measure_at_its_level(
    model: SemanticModel, grouping: str, measure: str
) -> None:
    """A region's subtotal is the measure by region; the grand total, the
    measure by nothing: both read the region the query's filter leaves out."""
    rows = _rows(
        model,
        {
            "select": {"dimensions": ["Region", "City"], "measures": [measure]},
            "where": IN_N_AND_S,
            "grouping": grouping,
        },
    )
    by_region = {
        r["Region"]: r[measure]
        for r in _rows(
            model,
            {"select": {"dimensions": ["Region"], "measures": [measure]}, "where": IN_N_AND_S},
        )
    }
    (everything,) = _rows(model, {"select": {"measures": [measure]}, "where": IN_N_AND_S})

    subtotals = {r["Region"]: r[measure] for r in rows if (r["_g_Region"], r["_g_City"]) == (0, 1)}
    (grand,) = [r[measure] for r in rows if (r["_g_Region"], r["_g_City"]) == (1, 1)]

    assert subtotals == {"n": Decimal(90), "s": Decimal(40)}
    assert subtotals == {region: by_region[region] for region in subtotals}
    assert grand == everything[measure] == Decimal(142)


def test_a_total_keeps_the_flags(model: SemanticModel) -> None:
    """The totals wrapper returned no flags, so the grand total and a NULL
    member's subtotal were the same row twice."""
    rows = _rows(
        model,
        {
            "select": {"dimensions": ["Region", "City"], "measures": ["Revenue Total"]},
            "where": IN_N_AND_S,
            "grouping": "rollup",
        },
    )

    assert {(r["_g_Region"], r["_g_City"]) for r in rows} == {(0, 0), (0, 1), (1, 1)}
    assert {r["Revenue Total"] for r in rows} == {Decimal(130)}
