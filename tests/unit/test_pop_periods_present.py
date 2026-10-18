"""A period-over-period query returns the periods with rows, and NULL groups compare.

Three defects in one place. The date spine's empty periods were rows of the
result (a gap month as a row of NULLs), which a plain query does not have.
With a dimension such a row had every dimension NULL, indistinguishable from
a real NULL group. And the prior period was matched on the dimensions with
``=``, which never matches a NULL group, so a sale without a region never had
a prior period.

Now only periods with rows are returned; the spine stays internal for finding
each period's prior one. A prior period without rows inside the range reads
empty, as the measure does over no rows (a count 0, a declared default); one
before the range stays unknown. NULL groups match their own prior period.
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
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Region: {code: region, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
dimensions:
  Region: {dataObject: Orders, column: Region, resultType: string}
  Order Date: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Revenue Or Zero:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    defaultValue: 0
  Big Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    dataType: "decimal(18, 2)"
    defaultValue: 1.234
    filters:
      - column: {dataObject: Orders, column: Amount}
        operator: gt
        values: [{dataType: float, valueFloat: 100}]
metrics:
  Big Revenue Ratio:
    type: period_over_period
    expression: '{[Big Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: ratio
  Next Orders Count Change:
    type: period_over_period
    expression: '{[Orders Count]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: 1
      offsetGrain: month
      comparison: difference
  Prior Revenue:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Prior Revenue Or Zero:
    type: period_over_period
    expression: '{[Revenue Or Zero]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Orders Count Change:
    type: period_over_period
    expression: '{[Orders Count]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id INTEGER, region VARCHAR, order_date DATE, amount DOUBLE)")
    # February has no orders; one order a month has no region.
    con.execute(
        "INSERT INTO orders VALUES"
        " (1, 'north', DATE '2026-01-10', 10), (2, NULL, DATE '2026-01-11', 5),"
        " (3, 'north', DATE '2026-03-10', 30), (4, 'south', DATE '2026-03-11', 7),"
        " (5, NULL, DATE '2026-03-12', 6), (6, NULL, DATE '2026-04-02', 4)"
    )
    return con


def _rows(con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple[object, ...]]:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return sorted(
        (
            tuple(
                float(v)
                if isinstance(v, int | float)
                else (str(v)[:7] if hasattr(v, "year") else v)
                for v in row
            )
            for row in con.execute(sql).fetchall()
        ),
        key=str,
    )


def test_only_periods_with_rows_are_returned(con: duckdb.DuckDBPyConnection) -> None:
    """February had a row of NULLs; a plain query has none, nor does this."""
    rows = _rows(con, {"select": {"dimensions": ["Order Date"], "measures": ["Prior Revenue"]}})
    assert [r[0] for r in rows] == ["2026-01", "2026-03", "2026-04"]


def test_an_empty_prior_period_reads_empty(con: duckdb.DuckDBPyConnection) -> None:
    """March's prior is February, inside the range and without orders: a
    count reads 0 (so the change is March's 3), a default reads 0, a plain sum
    stays NULL. January's prior is before the range: unknown."""
    rows = _rows(
        con,
        {
            "select": {
                "dimensions": ["Order Date"],
                "measures": ["Prior Revenue", "Prior Revenue Or Zero", "Orders Count Change"],
            }
        },
    )
    assert rows == [
        ("2026-01", None, None, None),
        ("2026-03", None, 0.0, 3.0),
        ("2026-04", 43.0, 43.0, -2.0),
    ]


def test_a_null_group_is_no_fake_row_and_has_its_prior(con: duckdb.DuckDBPyConnection) -> None:
    """By region: no February row with a NULL region (the spine's empty month
    used to be one), and April's NULL region reads March's NULL region, 6
    (``=`` never matched it)."""
    rows = _rows(
        con,
        {
            "select": {
                "dimensions": ["Order Date", "Region"],
                "measures": ["Prior Revenue", "Orders Count Change"],
            }
        },
    )
    assert rows == [
        ("2026-01", "north", None, None),
        ("2026-01", None, None, None),
        ("2026-03", "north", None, 1.0),
        ("2026-03", "south", None, 1.0),
        ("2026-03", None, None, 1.0),
        ("2026-04", None, 6.0, 0.0),
    ]


def test_a_time_filter_reads_empty_priors_from_the_look_back(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """From March on, the prior periods come from the look-back, which still
    covers February: empty there, so March's count change is 3."""
    rows = _rows(
        con,
        {
            "select": {"dimensions": ["Order Date"], "measures": ["Orders Count Change"]},
            "where": [{"field": "Order Date", "op": ">=", "value": "2026-03-01"}],
        },
    )
    assert rows == [("2026-03", 3.0), ("2026-04", -2.0)]


def test_a_later_period_past_the_range_is_unknown(con: duckdb.DuckDBPyConnection) -> None:
    """With a positive offset each month compares with the next. January's
    next is February, inside the range and empty: 2 - 0. April's is May, past
    the data: unknown, not empty (it read 1 - 0)."""
    rows = _rows(
        con, {"select": {"dimensions": ["Order Date"], "measures": ["Next Orders Count Change"]}}
    )
    assert rows == [("2026-01", 2.0), ("2026-03", 2.0), ("2026-04", None)]


def test_an_empty_prior_reads_the_default_typed_like_the_measure(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """No order passes the filter, so every month reads the default, cast to
    decimal(18, 2): 1.23. March's prior, February, has no rows: its default
    has to be cast the same, or the ratio reads 1.23 / 1.234 instead of 1."""
    rows = _rows(con, {"select": {"dimensions": ["Order Date"], "measures": ["Big Revenue Ratio"]}})
    assert rows == [("2026-01", None), ("2026-03", 1.0), ("2026-04", 1.0)]
