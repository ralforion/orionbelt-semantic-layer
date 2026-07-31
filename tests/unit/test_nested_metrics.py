"""Derived metrics that reference other metrics.

The planner inlines a metric's components into one expression. It used to
substitute one level only, so a metric over another metric emitted the inner
metric's own ``{[Name]}`` placeholder as a bare column - SQL that parses and
then fails at execution with ``Referenced column "Revenue" not found``.

A derived metric is now expanded in place, at any depth. A cumulative, window,
or period-over-period metric is computed by its own wrapper instead, so a
derived metric may reference a window metric (the wrapper projects it as a
column of its base CTE) but not the other two - ``parser/resolver.py`` refuses
those, and the arithmetic below is pinned by executing against DuckDB.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
name: nested_metrics

dataObjects:
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Date: {code: sale_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
      Cost: {code: cost, abstractType: float}

dimensions:
  Sale Month: {dataObject: Sales, column: Sale Date, resultType: date, timeGrain: month}

measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Amount]}'
  Total Cost:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Cost]}'
  Grand Total Revenue:
    resultType: float
    aggregation: sum
    total: true
    expression: '{[Sales].[Amount]}'

metrics:
  Margin:
    expression: '{[Revenue]} - {[Total Cost]}'
  Margin Pct:
    expression: '{[Margin]} / {[Revenue]}'
  Margin Pct Scaled:
    expression: '{[Margin Pct]} * 100'
  Guarded Margin:
    expression: 'CASE WHEN {[Revenue]} > 0 THEN {[Margin]} ELSE 0 END'
  Revenue Share:
    expression: '{[Revenue]} / {[Grand Total Revenue]}'
  Scaled Revenue Share:
    expression: '{[Revenue Share]} * 100'
  Revenue Rank:
    type: window
    windowFunction: dense_rank
    measure: Revenue
    orderDirection: desc
  Doubled Rank:
    expression: '{[Revenue Rank]} * 2'
  Doubled Rank Plus One:
    expression: '{[Doubled Rank]} + 1'
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Sale Month
  Revenue MoM:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Sale Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw = YAML(typ="safe").load(yaml_text)
    model, result = ReferenceResolver().resolve(raw)
    assert not result.errors, result.errors
    return model


def _compile(
    query: dict, yaml_text: str = MODEL_YAML, dialect: str = "duckdb"
) -> CompilationResult:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), dialect)


def _db() -> duckdb.DuckDBPyConnection:
    """January: revenue 100, cost 60. February: revenue 300, cost 100."""
    con = duckdb.connect()
    con.execute("CREATE TABLE sales (id VARCHAR, sale_date DATE, amount DOUBLE, cost DOUBLE)")
    con.execute(
        "INSERT INTO sales VALUES ('s1','2024-01-10',60,40), ('s2','2024-01-20',40,20),"
        " ('s3','2024-02-05',300,100)"
    )
    return con


def _rows(query: dict) -> list[tuple]:
    result = _compile(query)
    return _db().execute(result.sql).fetchall()


# --- the expansion ------------------------------------------------------


def test_metric_over_a_metric_resolves_to_real_aggregates() -> None:
    result = _compile({"select": {"dimensions": ["Sale Month"], "measures": ["Margin Pct"]}})
    # The inner metric's placeholder is gone; its components are inlined.
    assert '"Margin"' not in result.sql
    assert result.sql.count('SUM("Sales"."amount")') == 2

    rows = sorted((r[0].month, round(float(r[1]), 4)) for r in _db().execute(result.sql).fetchall())
    # January (100 - 60) / 100, February (300 - 100) / 300.
    assert rows == [(1, 0.4), (2, 0.6667)]


def test_three_levels_of_nesting() -> None:
    rows = sorted(
        (r[0].month, round(float(r[1]), 2))
        for r in _rows(
            {"select": {"dimensions": ["Sale Month"], "measures": ["Margin Pct Scaled"]}}
        )
    )
    assert rows == [(1, 40.0), (2, 66.67)]


def test_nesting_inside_a_case_expression() -> None:
    """The substitution walks every node, not just arithmetic and calls."""
    result = _compile({"select": {"dimensions": ["Sale Month"], "measures": ["Guarded Margin"]}})
    assert '"Margin"' not in result.sql

    rows = sorted((r[0].month, float(r[1])) for r in _db().execute(result.sql).fetchall())
    assert rows == [(1, 40.0), (2, 200.0)]


def test_the_inner_metric_stays_queryable_on_its_own() -> None:
    rows = sorted(
        (r[0].month, float(r[1]))
        for r in _rows({"select": {"dimensions": ["Sale Month"], "measures": ["Margin"]}})
    )
    assert rows == [(1, 40.0), (2, 200.0)]


@pytest.mark.parametrize(
    "dialect",
    ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "mysql", "postgres", "snowflake"],
)
def test_generated_sql_is_valid_for_every_dialect(dialect: str) -> None:
    result = _compile(
        {"select": {"dimensions": ["Sale Month"], "measures": ["Margin Pct Scaled"]}},
        dialect=dialect,
    )
    assert result.sql_valid, (dialect, result.warnings)


# --- composing with the aggregate-mode wrappers -------------------------


def test_nesting_over_a_total_component() -> None:
    """The totals wrapper has to see the ``total: true`` measure through both metrics."""
    result = _compile(
        {"select": {"dimensions": ["Sale Month"], "measures": ["Scaled Revenue Share"]}}
    )
    assert "OVER ()" in result.sql

    rows = sorted((r[0].month, round(float(r[1]), 2)) for r in _db().execute(result.sql).fetchall())
    # Share of the 400 grand total, not of the month's own revenue.
    assert rows == [(1, 25.0), (2, 75.0)]


def test_nesting_over_a_window_metric() -> None:
    """The window pass must fire even two metrics away from the window call."""
    result = _compile(
        {"select": {"dimensions": ["Sale Month"], "measures": ["Doubled Rank Plus One"]}}
    )
    assert "DENSE_RANK" in result.sql.upper()

    rows = sorted((r[0].month, int(r[1])) for r in _db().execute(result.sql).fetchall())
    # February ranks first (300 > 100): 1*2+1; January 2*2+1.
    assert rows == [(1, 5), (2, 3)]


def test_nesting_alongside_a_cumulative_metric() -> None:
    rows = sorted(
        (r[0].month, round(float(r[1]), 2), float(r[2]))
        for r in _rows(
            {
                "select": {
                    "dimensions": ["Sale Month"],
                    "measures": ["Margin Pct Scaled", "Running Revenue"],
                }
            }
        )
    )
    assert rows == [(1, 40.0, 100.0), (2, 66.67, 400.0)]


def test_nesting_alongside_a_period_over_period_metric() -> None:
    """PoP rebuilds the projection from a date spine, where a metric's own
    placeholders name columns nothing projects."""
    rows = sorted(
        (r[0].month, round(float(r[1]), 2), None if r[2] is None else float(r[2]))
        for r in _rows(
            {
                "select": {
                    "dimensions": ["Sale Month"],
                    "measures": ["Margin Pct Scaled", "Revenue MoM"],
                }
            }
        )
    )
    assert rows == [(1, 40.0, None), (2, 66.67, 200.0)]


# --- multi-fact (CFL) ---------------------------------------------------


CFL_YAML = """
version: 1.0
name: nested_cfl

dataObjects:
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Amount: {code: amount, abstractType: float}
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: string, primaryKey: true}
      Refund: {code: refund, abstractType: float}

measures:
  Sales Amount:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Amount]}'
  Refund Amount:
    resultType: float
    aggregation: sum
    expression: '{[Returns].[Refund]}'

metrics:
  Net: {expression: '{[Sales Amount]} - {[Refund Amount]}'}
  Net Doubled: {expression: '{[Net]} * 2'}
"""


def _cfl_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE sales (id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO sales VALUES ('s1',100), ('s2',50)")
    con.execute("CREATE TABLE returns (id VARCHAR, refund DOUBLE)")
    con.execute("INSERT INTO returns VALUES ('r1',30)")
    return con


@pytest.mark.parametrize(
    ("measures", "expected"),
    [
        (["Net"], (120.0,)),
        (["Net Doubled"], (240.0,)),
        (["Sales Amount", "Net Doubled"], (150.0, 240.0)),
    ],
)
def test_nested_metric_across_two_facts(measures: list[str], expected: tuple[float, ...]) -> None:
    """Each leaf needs its own UNION ALL leg, nesting or not.

    Attributing a component to a leg read its ``columns:`` list, so a component
    declared as an ``expression:`` fell back to the base object and landed in a
    leg whose FROM never joined its table.
    """
    result = _compile({"select": {"dimensions": [], "measures": measures}}, CFL_YAML)
    assert "composite_01" in result.sql

    rows = _cfl_db().execute(result.sql).fetchall()
    assert tuple(float(v) for v in rows[0]) == expected
