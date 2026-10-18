"""A filter on a conformed fact is applied inside its subquery, on every engine.

An anchored measure aggregates an independent fact to the key it shares with
the anchor. A query filter, a measure filter, or a filter on an object reached
through that fact chooses the fact's rows in the subquery, before aggregation;
in a union, a leg that reaches the filter itself applies it in its own WHERE.
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

CALENDAR = "conf_filter_calendar"
REASONS = "conf_filter_reasons"
SALES = "conf_filter_sales"
RETURNS = "conf_filter_returns"

CALENDAR_ROWS = [("d1", 2024), ("d2", 2024)]
REASON_ROWS = [("R1", "damage"), ("R2", "other")]
# d1 sells 2 + 3, d2 sells 6.
SALE_ROWS = [("s1", "d1", 2.0), ("s2", "d1", 3.0), ("s3", "d2", 6.0)]
# d1 returns 4 (keep, damage) and 5 (drop, other), d2 returns 7 (keep, other).
RETURN_ROWS = [
    ("r1", "d1", "R1", "keep", 4.0),
    ("r2", "d1", "R2", "drop", 5.0),
    ("r3", "d2", "R2", "keep", 7.0),
]

MODEL_YAML = """
version: "1.0"
name: conformed_filters_vendor
dataObjects:
  Calendar:
    code: {calendar}
    schema: '{schema}'
    columns:
      Date Key: {{code: datekey, abstractType: string, primaryKey: true}}
      Year: {{code: year, abstractType: int}}
  Reasons:
    code: {reasons}
    schema: '{schema}'
    columns:
      Reason ID: {{code: id, abstractType: string, primaryKey: true}}
      Category: {{code: category, abstractType: string}}
  Sales:
    code: {sales}
    schema: '{schema}'
    columns:
      Sale ID: {{code: id, abstractType: string, primaryKey: true}}
      Sale Date Key: {{code: datekey, abstractType: string}}
      Sale Qty: {{code: qty, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
  Returns:
    code: {returns}
    schema: '{schema}'
    columns:
      Return ID: {{code: id, abstractType: string, primaryKey: true}}
      Return Date Key: {{code: datekey, abstractType: string}}
      Return Reason ID: {{code: reason_id, abstractType: string}}
      Return Status: {{code: status, abstractType: string}}
      Return Qty: {{code: qty, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Reasons
        columnsFrom: [Return Reason ID]
        columnsTo: [Reason ID]
dimensions:
  Year: {{dataObject: Calendar, column: Year, resultType: int}}
  Day: {{dataObject: Calendar, column: Date Key, resultType: string}}
  Return Status: {{dataObject: Returns, column: Return Status, resultType: string}}
  Reason Category: {{dataObject: Reasons, column: Category, resultType: string}}
measures:
  Anchored Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{{[Sales].[Sale Qty]}} * {{[Returns].[Return Qty]}}'
  Kept Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{{[Sales].[Sale Qty]}} * {{[Returns].[Return Qty]}}'
    filters:
      - column: {{dataObject: Returns, column: Return Status}}
        operator: equals
        values: [{{dataType: string, valueString: keep}}]
  Returned Qty:
    resultType: float
    aggregation: sum
    expression: '{{[Returns].[Return Qty]}}'
metrics:
  Daily Cross:
    type: reaggregate
    measure: Anchored Cross
    per: [Day]
    aggregation: avg
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, CALENDAR, [("datekey", 0), ("year", 2)], CALENDAR_ROWS)
    _create(target, REASONS, [("id", 0), ("category", 0)], REASON_ROWS)
    _create(target, SALES, [("id", 0), ("datekey", 0), ("qty", 1)], SALE_ROWS)
    _create(
        target,
        RETURNS,
        [("id", 0), ("datekey", 0), ("reason_id", 0), ("status", 0), ("qty", 1)],
        RETURN_ROWS,
    )
    yaml_text = MODEL_YAML.format(
        calendar=CALENDAR,
        reasons=REASONS,
        sales=SALES,
        returns=RETURNS,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _values(
    target: VendorTarget, model: SemanticModel, measures: list[str], where: list[dict]
) -> list[Decimal | None]:
    rows = _run(
        target,
        model,
        QueryObject(**{"select": {"dimensions": ["Year"], "measures": measures}, "where": where}),
    )
    assert len(rows) == 1, rows
    return [_number(rows[0][name.lower()]) for name in measures]


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    keep = [{"field": "Return Status", "op": "equals", "value": "keep"}]
    damage = [{"field": "Reason Category", "op": "equals", "value": "damage"}]
    # Unfiltered: d1 (2 + 3) * 9, d2 6 * 7.
    assert _values(target, model, ["Anchored Cross"], []) == [Decimal(87)]
    # Kept returns: d1 (2 + 3) * 4, d2 6 * 7; the measure filter alike.
    assert _values(target, model, ["Anchored Cross", "Kept Cross"], keep) == [
        Decimal(62),
        Decimal(62),
    ]
    # Reasons joined inside the subquery: only d1's damaged 4.
    assert _values(target, model, ["Anchored Cross"], damage) == [Decimal(20)]
    # An EXISTS correlated to Returns, inside its subquery: r2, r3 have no
    # damage reason, d1 5 * 5 + d2 6 * 7.
    nonexists = [
        {
            "field": "Returns.Return ID",
            "op": "nonexists",
            "subquery": {
                "dataObject": "Reasons",
                "filter": [{"field": "Category", "op": "equals", "value": "damage"}],
            },
        }
    ]
    assert _values(target, model, ["Anchored Cross"], nonexists) == [Decimal(67)]
    # Per day, then averaged: (20 + 42) / 2.
    assert _values(target, model, ["Daily Cross"], keep) == [Decimal(31)]
    # A union leg on Returns applies the filter in its own WHERE: 4 + 7.
    assert _values(target, model, ["Anchored Cross", "Returned Qty"], keep) == [
        Decimal(62),
        Decimal(11),
    ]


def test_duckdb_conformed_filters(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_conformed_filters(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_conformed_filters(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_conformed_filters(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_conformed_filters(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_conformed_filters(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_conformed_filters(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_conformed_filters(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
