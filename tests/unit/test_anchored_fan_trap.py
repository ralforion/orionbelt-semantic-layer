"""A summed term reading only a conformed fact repeats once per anchor row.

Anchored on Returns, each sale date's sales are conformed to one total per
date and read once per return. ``SUM(r * S)`` is right: ``R * S`` per date.
``SUM(r - S)`` is not: ``R - n * S``, and a date without returns loses its
sales. The query carries a ``FAN_TRAP_RISK`` warning naming the measure.

======  ======================  =========
date    returns                 sales
======  ======================  =========
``d1``  1, 2, 3                 10
``d2``  *(none)*                5
======  ======================  =========
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: anchored_fan_trap
dataObjects:
  Dates:
    code: dates
    columns:
      Date Key: {{code: id, abstractType: string, primaryKey: true}}
  Sales:
    code: sales
    columns:
      Sale Id: {{code: id, abstractType: string, primaryKey: true}}
      Sale Date: {{code: date_id, abstractType: string}}
      Sale Qty: {{code: qty, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Sale Date]
        columnsTo: [Date Key]
  Returns:
    code: returns
    columns:
      Return Id: {{code: id, abstractType: string, primaryKey: true}}
      Return Date: {{code: date_id, abstractType: string}}
      Return Qty: {{code: qty, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Return Date]
        columnsTo: [Date Key]
measures:
  Net:
    aggregation: {aggregation}
    resultType: float
{anchor}    expression: '{expression}'
{filters}"""

R, S = "{[Returns].[Return Qty]}", "{[Sales].[Sale Qty]}"
LARGE_RETURNS = """    filters:
      - column: {dataObject: Returns, column: Return Qty}
        operator: gt
        values: [{dataType: float, valueFloat: 1}]
"""


def _compile(
    expression: str,
    anchor: str | None = "Returns",
    aggregation: str = "sum",
    filters: str = "",
) -> CompilationResult:
    yaml_text = MODEL_YAML.format(
        aggregation=aggregation,
        anchor=f"    anchor: {anchor}\n" if anchor else "",
        expression=expression,
        filters=filters,
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject(select={"dimensions": [], "measures": ["Net"]})
    return CompilationPipeline().compile(query, model, "duckdb")


def _fan_traps(compiled: CompilationResult) -> list[dict]:
    return [w.context for w in compiled.warnings if w.code == "FAN_TRAP_RISK"]


@pytest.mark.parametrize(
    "expression",
    [f"{R} - {S}", f"{S} + {R}", f"-{S} + {R}", f"({R} - {S}) * 2", f"{R} + {S} / 2"],
    ids=["difference", "sum", "negated", "scaled", "halved"],
)
def test_a_term_without_an_anchor_column_warns(expression: str) -> None:
    assert _fan_traps(_compile(expression)) == [
        {"measure": "Net", "anchor": "Returns", "conformed": ["Sales"]}
    ]


@pytest.mark.parametrize(
    "expression",
    [f"{R} * {S}", f"{R} * ({S} + 1)", f"{R} / {S}", f"{S} / {R}", f"({S} - 1) * {R}"],
    ids=["product", "distributed", "ratio", "inverse", "factored"],
)
def test_every_term_with_an_anchor_column_does_not_warn(expression: str) -> None:
    assert _fan_traps(_compile(expression)) == []


def test_a_measure_filter_still_repeats_the_term() -> None:
    """The filter wraps the expression in a CASE per anchor row: the sales
    are still subtracted once for each return passing it."""
    assert _fan_traps(_compile(f"{R} - {S}", filters=LARGE_RETURNS)) != []


@pytest.mark.parametrize(
    ("anchor", "aggregation"),
    [(None, "sum"), ("Dates", "sum"), ("Returns", "avg")],
    ids=["shared-key", "anchored-on-the-key", "avg"],
)
def test_one_row_per_key_or_a_non_sum_does_not_warn(anchor: str | None, aggregation: str) -> None:
    """Conformed to the date, there is one row per key, so nothing repeats.
    An AVG is evaluated over the anchor's rows by definition."""
    assert _fan_traps(_compile(f"{R} - {S}", anchor, aggregation)) == []


def test_the_warned_value_is_the_repeated_one() -> None:
    """The warning describes what the SQL computes: 6 - 3 * 10 on d1, and
    d2's sales are not read."""
    con = duckdb.connect()
    con.execute("CREATE TABLE dates AS SELECT * FROM (VALUES ('d1'), ('d2')) t(id)")
    con.execute(
        "CREATE TABLE sales AS SELECT * FROM (VALUES"
        " ('s1', 'd1', 10.0), ('s2', 'd2', 5.0)) t(id, date_id, qty)"
    )
    con.execute(
        "CREATE TABLE returns AS SELECT * FROM (VALUES"
        " ('r1', 'd1', 1.0), ('r2', 'd1', 2.0), ('r3', 'd1', 3.0)) t(id, date_id, qty)"
    )
    compiled = _compile(f"{R} - {S}")
    assert con.execute(compiled.sql).fetchall() == [(Decimal("-24.00"),)]
    assert _fan_traps(compiled)
