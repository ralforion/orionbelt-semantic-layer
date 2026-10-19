"""``fill: true`` returns every period of the query's time dimension.

=========  ======  =============  ======
order      region  date           amount
=========  ======  =============  ======
``o1``     r1      2024-01-05     1
``o2``     r2      2024-03-09     2
``o3``     r1      *(none)*       4
``o4``     *(none)* 2024-03-01    8
=========  ======  =============  ======

February has no orders, and ``o3`` no date: a fill adds the first and keeps
the second.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: fill
dataObjects:
  Calendar:
    code: calendar
    columns:
      Day: {code: d, abstractType: date, primaryKey: true}
  Regions:
    code: regions
    columns:
      Region Id: {code: id, abstractType: string, primaryKey: true}
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Order Region: {code: region_id, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Order Time: {code: order_time, abstractType: timestamp}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Order Region]
        columnsTo: [Region Id]
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Order Date]
        columnsTo: [Day]
  Returns:
    code: returns
    columns:
      Return Id: {code: id, abstractType: string, primaryKey: true}
      Return Date: {code: return_date, abstractType: date}
      Refund: {code: refund, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date]
        columnsTo: [Day]
dimensions:
  Region: {dataObject: Regions, column: Region Id, resultType: string}
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
  Order Day: {dataObject: Orders, column: Order Date, resultType: date}
  Order Hour: {dataObject: Orders, column: Order Time, resultType: timestamp, timeGrain: hour}
  Month: {dataObject: Calendar, column: Day, resultType: date, timeGrain: month}
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
  Order Total:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    total: true
  Refunds:
    columns: [{dataObject: Returns, column: Refund}]
    aggregation: sum
    resultType: float
metrics:
  Average Order:
    expression: '{[Revenue]} / {[Orders Count]}'
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
"""

JAN, FEB, MAR, APR = date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1), date(2024, 4, 1)
DEC = date(2023, 12, 1)


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions AS SELECT * FROM (VALUES ('r1'), ('r2')) t(id)")
    con.execute(
        "CREATE TABLE calendar AS SELECT * FROM (VALUES"
        " (DATE '2024-01-05'), (DATE '2024-03-01'), (DATE '2024-03-09'), (DATE '2024-04-02'))"
        " t(d)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES"
        " ('o1', 'r1', DATE '2024-01-05', 1.0),"
        " ('o2', 'r2', DATE '2024-03-09', 2.0),"
        " ('o3', 'r1', NULL, 4.0),"
        " ('o4', NULL, DATE '2024-03-01', 8.0))"
        " t(id, region_id, order_date, amount)"
    )
    con.execute("ALTER TABLE orders ADD COLUMN order_time TIMESTAMP")
    con.execute(
        "CREATE TABLE returns AS SELECT * FROM (VALUES"
        " ('x1', DATE '2024-04-02', 5.0)) t(id, return_date, refund)"
    )
    return con


def _rows(con: duckdb.DuckDBPyConnection, model: SemanticModel, query: dict) -> list[tuple]:
    compiled = CompilationPipeline().compile(QueryObject(**query, fill=True), model, "duckdb")
    return con.execute(compiled.sql).fetchall()


def _d(*values: float | None) -> tuple[Decimal | None, ...]:
    return tuple(None if v is None else Decimal(f"{v:.2f}") for v in values)


def test_an_empty_period_is_added_and_a_row_without_one_kept(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """February counts 0, reads the default, and is NULL otherwise; the order
    without a date stays its own row."""
    rows = _rows(
        con,
        model,
        {
            "select": {
                "dimensions": ["Order Month"],
                "measures": ["Revenue", "Revenue Or Zero", "Orders Count", "Average Order"],
            },
            "orderBy": [{"field": "Order Month"}],
        },
    )
    assert rows == [
        (JAN, *_d(1, 1), 1, Decimal("1.000000")),
        (FEB, *_d(None, 0), 0, None),
        (MAR, *_d(10, 10), 2, Decimal("5.000000")),
        (None, *_d(4, 4), 1, Decimal("4.000000")),
    ]


@pytest.mark.parametrize(
    ("where", "periods"),
    [
        (
            [
                {"field": "Order Month", "op": "gte", "value": "2023-12-15"},
                {"field": "Order Month", "op": "lt", "value": "2024-05-01"},
            ],
            [DEC, JAN, FEB, MAR, APR],
        ),
        (
            [{"field": "Order Month", "op": "between", "value": ["2024-02-10", "2024-04-30"]}],
            [FEB, MAR, APR],
        ),
        ([{"field": "Order Month", "op": "lte", "value": "2024-04-01"}], [JAN, FEB, MAR, APR]),
        ([{"field": "Order Month", "op": "lt", "value": "2024-04-01"}], [JAN, FEB, MAR]),
        ([{"field": "Order Month", "op": "gt", "value": "2023-11-30"}], [DEC, JAN, FEB, MAR]),
    ],
    ids=["gte-lt", "between", "lte", "lt-a-period-start", "gt"],
)
def test_the_where_bounds_set_the_range(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, where: list, periods: list
) -> None:
    """A strict upper bound on a period's first day leaves that period out."""
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Month"], "measures": ["Orders Count"]},
            "where": where,
            "orderBy": [{"field": "Order Month"}],
        },
    )
    assert [row[0] for row in rows] == periods


def test_a_bound_inside_the_data_does_not_cut_it_off(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """``Order Day`` is the same column without a grain: its filter is no bound
    on the months, and the range is the data's."""
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Month"], "measures": ["Orders Count"]},
            "where": [{"field": "Order Day", "op": "gte", "value": "2024-01-01"}],
            "orderBy": [{"field": "Order Month"}],
        },
    )
    assert rows == [(JAN, 1), (FEB, 0), (MAR, 2)]


def test_every_member_gets_every_period(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """The NULL region (``o4``) is a member like the others."""
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Region", "Order Month"], "measures": ["Revenue"]},
            "where": [
                {"field": "Order Month", "op": "between", "value": ["2024-01-01", "2024-03-31"]}
            ],
            "orderBy": [{"field": "Region"}, {"field": "Order Month"}],
        },
    )
    assert rows == [
        ("r1", JAN, *_d(1)),
        ("r1", FEB, *_d(None)),
        ("r1", MAR, *_d(None)),
        ("r2", JAN, *_d(None)),
        ("r2", FEB, *_d(None)),
        ("r2", MAR, *_d(2)),
        (None, JAN, *_d(None)),
        (None, FEB, *_d(None)),
        (None, MAR, *_d(8)),
    ]


def test_a_query_without_measures_lists_every_period(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    rows = _rows(
        con,
        model,
        {"select": {"dimensions": ["Order Month"]}, "orderBy": [{"field": "Order Month"}]},
    )
    assert rows == [(JAN,), (FEB,), (MAR,), (None,)]


def test_limit_applies_to_the_filled_rows(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Month"], "measures": ["Orders Count"]},
            "orderBy": [{"field": "Order Month"}],
            "limit": 2,
        },
    )
    assert rows == [(JAN, 1), (FEB, 0)]


def test_independent_facts_are_filled_together(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Month"], "measures": ["Revenue", "Refunds"]},
            "orderBy": [{"field": "Month"}],
        },
    )
    # The order without a date (o3) is in no month of the calendar.
    assert rows == [
        (JAN, *_d(1, None)),
        (FEB, *_d(None, None)),
        (MAR, *_d(10, None)),
        (APR, *_d(None, 5)),
        (None, *_d(4, None)),
    ]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        (
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}},
            "FILL_REQUIRES_ONE_TIME_DIMENSION",
        ),
        (
            {"select": {"dimensions": ["Order Month", "Month"], "measures": ["Revenue"]}},
            "FILL_REQUIRES_ONE_TIME_DIMENSION",
        ),
        ({"select": {"fields": ["Orders.Amount"]}}, "FILL_REQUIRES_ONE_TIME_DIMENSION"),
        (
            {"select": {"dimensions": ["Order Hour"], "measures": ["Revenue"]}},
            "FILL_GRAIN_NOT_SUPPORTED",
        ),
        (
            {
                "select": {"dimensions": ["Order Month"], "measures": ["Revenue"]},
                "grouping": "rollup",
            },
            "FILL_COMBINATION_NOT_SUPPORTED",
        ),
        (
            {
                "select": {"dimensions": ["Order Month"], "measures": ["Revenue"]},
                "having": [{"field": "Revenue", "op": "gt", "value": 1}],
            },
            "FILL_COMBINATION_NOT_SUPPORTED",
        ),
        (
            {"select": {"dimensions": ["Order Month"], "measures": ["Order Total"]}},
            "FILL_COMBINATION_NOT_SUPPORTED",
        ),
        (
            {"select": {"dimensions": ["Order Month"], "measures": ["Running Revenue"]}},
            "FILL_COMBINATION_NOT_SUPPORTED",
        ),
    ],
    ids=["no-time", "two-times", "raw", "hour", "rollup", "having", "total", "cumulative"],
)
def test_refused(model: SemanticModel, query: dict, code: str) -> None:
    with pytest.raises(ResolutionError) as raised:
        CompilationPipeline().compile(QueryObject(**query, fill=True), model, "duckdb")
    assert code in {error.code for error in raised.value.errors}
