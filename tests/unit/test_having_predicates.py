"""Every HAVING predicate names a measure by its aggregate, never its alias.

Postgres rejects a SELECT alias in HAVING. The planners swap each measure
reference for its aggregate, and used to walk only ``BinaryOp`` and
``FunctionCall``: a pattern filter left ``HAVING "First Country" LIKE 'U%'``
once LIKE became its own node, and ``BETWEEN``, ``IN`` and ``IS NULL`` on a
measure were never reached.
"""

from __future__ import annotations

import re

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """\
version: 1.0

dataObjects:
  Dates:
    code: DATES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int, primaryKey: true}
      Month: {code: MONTH, abstractType: int}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Country: {code: COUNTRY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

  Refunds:
    code: REFUNDS
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Refund: {code: REFUND, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

dimensions:
  Month: {dataObject: Dates, column: Month, resultType: int}

measures:
  First Country:
    columns: [{dataObject: Sales, column: Country}]
    resultType: string
    aggregation: min
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Refund Amount:
    columns: [{dataObject: Refunds, column: Refund}]
    resultType: float
    aggregation: sum
"""

_MEASURES = ("First Country", "Sales Amount", "Refund Amount")


def _model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _having(measures: list[str], qf: QueryFilter) -> str:
    query = QueryObject(select=QuerySelect(dimensions=["Month"], measures=measures), having=[qf])
    sql = CompilationPipeline().compile(query, _model(), "postgres").sql
    match = re.search(r"\bHAVING\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|\Z)", sql, re.S)
    assert match, sql
    return match.group(1)


@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        ("First Country", FilterOperator.LIKE, "U%"),
        ("First Country", FilterOperator.NOT_LIKE, "U%"),
        ("First Country", FilterOperator.ILIKE, "u%"),
        ("First Country", FilterOperator.CONTAINS, "U"),
        ("First Country", FilterOperator.STARTS_WITH, "U"),
        ("Sales Amount", FilterOperator.BETWEEN, [10, 20]),
        ("Sales Amount", FilterOperator.IN_LIST, [10, 20]),
        ("Sales Amount", FilterOperator.IS_NULL, None),
    ],
)
@pytest.mark.parametrize(
    "measures",
    [["First Country", "Sales Amount"], ["First Country", "Sales Amount", "Refund Amount"]],
    ids=["star", "cfl"],
)
def test_having_uses_the_aggregate(
    measures: list[str], field: str, op: FilterOperator, value: object
) -> None:
    having = _having(measures, QueryFilter(field=field, op=op, value=value))
    # Unqualified: CFL's aggregate reads the composite CTE's column of that name.
    assert not [m for m in _MEASURES if re.search(rf'(?<!\.)"{m}"', having)], having
    assert ("MIN(" if field == "First Country" else "SUM(") in having, having
