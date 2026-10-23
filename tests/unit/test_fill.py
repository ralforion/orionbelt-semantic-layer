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
  Order Time Month:
    dataObject: Orders
    column: Order Time
    resultType: timestamp
    timeGrain: month
  Order Week: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: week}
  Order Ref: {dataObject: Orders, column: Order Id, resultType: string}
  Return Ref: {dataObject: Returns, column: Return Id, resultType: string}
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
  Rolling Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
    window: 2
  Revenue QTD:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
    grainToDate: quarter
  Running Average Orders:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    cumulativeType: avg
  Running Revenue By Time:
    type: cumulative
    measure: Revenue
    timeDimension: Order Time Month
  Running Period Count:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    cumulativeType: count
  Rolling Period Count:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    cumulativeType: count
    window: 1
  Period Count QTD:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    cumulativeType: count
    grainToDate: quarter
  Average Per Order:
    type: reaggregate
    measure: Revenue
    per: [Order Ref]
    aggregation: avg
  Running Average Per Order:
    type: cumulative
    measure: Average Per Order
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
    con.execute("UPDATE orders SET order_time = order_date + INTERVAL 10 HOUR")
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
                {"field": "Order Month", "op": "gte", "value": "2023-12-01"},
                {"field": "Order Month", "op": "lt", "value": "2024-05-01"},
            ],
            [DEC, JAN, FEB, MAR, APR],
        ),
        (
            [
                {"field": "Order Month", "op": "gte", "value": "2023-12-15"},
                {"field": "Order Month", "op": "lte", "value": "2024-04-15"},
            ],
            [JAN, FEB, MAR, APR],
        ),
        (
            [{"field": "Order Month", "op": "between", "value": ["2024-02-10", "2024-04-30"]}],
            [MAR, APR],
        ),
        ([{"field": "Order Month", "op": "lte", "value": "2024-04-01"}], [JAN, FEB, MAR, APR]),
        ([{"field": "Order Month", "op": "lt", "value": "2024-04-01"}], [JAN, FEB, MAR]),
        ([{"field": "Order Month", "op": "gt", "value": "2023-11-30"}], [DEC, JAN, FEB, MAR]),
    ],
    ids=["gte-lt", "inside-periods", "between", "lte", "lt-a-period-start", "gt"],
)
def test_the_where_bounds_set_the_range(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, where: list, periods: list
) -> None:
    """A filter on ``Order Month`` compares months: ``>= Dec 15`` starts in
    January, as December starts before it, and ``<= Apr 15`` ends in April."""
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


def test_bounds_in_an_and_group_set_the_range(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """The bounds read the same inside ``logic: and`` as at the top level."""
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Month"], "measures": ["Orders Count"]},
            "where": [
                {
                    "logic": "and",
                    "filters": [
                        {"field": "Order Month", "op": "gte", "value": "2023-12-01"},
                        {"field": "Order Month", "op": "lt", "value": "2024-05-01"},
                    ],
                }
            ],
            "orderBy": [{"field": "Order Month"}],
        },
    )
    assert [row[0] for row in rows] == [DEC, JAN, FEB, MAR, APR]


def test_bounds_in_an_and_group_with_another_column_set_the_range(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """A group that also filters the region has no single subject; its date
    conjuncts are bounds all the same."""
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Month"], "measures": ["Orders Count"]},
            "where": [
                {
                    "logic": "and",
                    "filters": [
                        {"field": "Order Month", "op": "gte", "value": "2023-12-01"},
                        {"field": "Region", "op": "equals", "value": "r1"},
                        {"field": "Order Month", "op": "lt", "value": "2024-05-01"},
                    ],
                }
            ],
            "orderBy": [{"field": "Order Month"}],
        },
    )
    assert rows == [(DEC, 0), (JAN, 1), (FEB, 0), (MAR, 0), (APR, 0)]


@pytest.mark.parametrize(
    ("bound", "periods"),
    [
        (("lt", "2024-04-01 12:00:00"), [JAN, FEB, MAR, APR]),
        (("lt", "2024-04-01 00:00:00"), [JAN, FEB, MAR]),
        (("gt", "2023-12-31 12:00:00"), [JAN, FEB, MAR]),
    ],
    ids=["lt-inside-a-period", "lt-a-period-start", "gt-inside-a-period"],
)
def test_a_strict_timestamp_bound_compares_the_period(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, bound: tuple, periods: list
) -> None:
    """April starts at 00:00, before ``2024-04-01 12:00`` and not before
    ``2024-04-01 00:00``; December starts before ``2023-12-31 12:00``."""
    op, value = bound
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Time Month"], "measures": ["Orders Count"]},
            "where": [{"field": "Order Time Month", "op": op, "value": value}],
            "orderBy": [{"field": "Order Time Month"}],
        },
    )
    assert [row[0].date() for row in rows if row[0] is not None] == periods


def test_a_weekly_fill_reads_its_bounds(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Week"], "measures": ["Orders Count"]},
            "where": [
                {"field": "Order Week", "op": "between", "value": ["2024-02-20", "2024-03-10"]}
            ],
            "orderBy": [{"field": "Order Week"}],
        },
    )
    assert rows == [
        (date(2024, 2, 26), 1),
        (date(2024, 3, 4), 1),
    ]


def test_a_coalesced_dimension_is_a_member(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """Each leg's reference is one ``Ref`` member, filled like any other."""
    rows = _rows(
        con,
        model,
        {
            "select": {
                "dimensions": [{"coalesce": ["Order Ref", "Return Ref"], "as": "Ref"}, "Month"],
                "measures": ["Revenue", "Refunds"],
            },
            "orderBy": [{"field": "Ref"}, {"field": "Month"}],
        },
    )
    assert [row for row in rows if row[0] == "x1"] == [
        ("x1", JAN, *_d(None, None)),
        ("x1", FEB, *_d(None, None)),
        ("x1", MAR, *_d(None, None)),
        ("x1", APR, *_d(None, 5)),
    ]
    assert len(rows) == 5 * 4 + 1  # five refs over four months, o3 without one


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


CUMULATIVE = ["Running Revenue", "Rolling Revenue", "Revenue QTD", "Running Average Orders"]


def _dated(rows: list[tuple], axis: int = 0) -> list[tuple]:
    """The rows with a period, in order, numbers to two places: an undated
    row's running value is the cumulative pass's own, not something the fill
    adds, and a time filter types the running values float."""
    dated = [
        tuple(Decimal(f"{v:.2f}") if isinstance(v, float | Decimal) else v for v in row)
        for row in rows
        if row[axis] is not None
    ]
    return sorted(dated, key=lambda r: tuple((v is None, v) for v in r[: axis + 1]))


def test_an_empty_period_reads_its_cumulative_window_as_of_itself(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """February carries the running total, the rolling window and the quarter
    on from January; a period with rows reads what it reads without the fill."""
    query = {"select": {"dimensions": ["Order Month"], "measures": CUMULATIVE}}
    filled = _dated(_rows(con, model, query))
    assert filled == [
        (JAN, *_d(1, 1, 1, 1)),
        (FEB, *_d(1, 1, 1, 1)),
        (MAR, *_d(11, 10, 11, 1.5)),
    ]
    compiled = CompilationPipeline().compile(QueryObject(**query), model, "duckdb")
    unfilled = _dated(con.execute(compiled.sql).fetchall())
    assert [row for row in filled if row[0] != FEB] == unfilled


def test_a_cumulative_window_reads_past_the_filter_and_the_data(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """February reads January before the filter; April and May, after the
    data, keep the running total while the rolling window and the quarter
    run out."""
    rows = _rows(
        con,
        model,
        {
            "select": {"dimensions": ["Order Month"], "measures": CUMULATIVE},
            "where": [
                {"field": "Order Month", "op": "between", "value": ["2024-02-01", "2024-05-31"]}
            ],
        },
    )
    assert _dated(rows) == [
        (FEB, *_d(1, 1, 1, 1)),
        (MAR, *_d(11, 10, 11, 1.5)),
        (APR, *_d(11, 10, None, 1.5)),
        (date(2024, 5, 1), *_d(11, None, None, 1.5)),
    ]


def test_a_cumulative_window_reads_its_own_member(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """Each region's empty months read that region's earlier months only, and
    a timestamp dimension fills like a date."""
    rows = _rows(
        con,
        model,
        {
            "select": {
                "dimensions": ["Region", "Order Month"],
                "measures": ["Running Revenue", "Rolling Revenue"],
            }
        },
    )
    assert _dated(rows, axis=1) == [
        ("r1", JAN, *_d(1, 1)),
        ("r1", FEB, *_d(1, 1)),
        ("r1", MAR, *_d(1, None)),
        ("r2", JAN, *_d(None, None)),
        ("r2", FEB, *_d(None, None)),
        ("r2", MAR, *_d(2, 2)),
        (None, JAN, *_d(None, None)),
        (None, FEB, *_d(None, None)),
        (None, MAR, *_d(8, 8)),
    ]
    by_time = _rows(
        con,
        model,
        {"select": {"dimensions": ["Order Time Month"], "measures": ["Running Revenue By Time"]}},
    )
    assert [(r[0].date(), *r[1:]) for r in _dated(by_time)] == [
        (JAN, *_d(1)),
        (FEB, *_d(1)),
        (MAR, *_d(11)),
    ]


COUNTS = ["Running Period Count", "Rolling Period Count", "Period Count QTD"]


def test_a_cumulative_count_over_no_period_is_null(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """A count reads 0 over no rows, where its window here reads no period:
    December before the first one, February outside a one-month window, April
    and May in a quarter without one. ``asOf`` answers NULL there too."""
    query = {
        "select": {"dimensions": ["Order Month"], "measures": COUNTS},
        "where": [{"field": "Order Month", "op": "between", "value": ["2023-12-01", "2024-05-31"]}],
    }
    assert _dated(_rows(con, model, query)) == [
        (DEC, None, None, None),
        (JAN, 1, 1, 1),
        (FEB, 1, None, 1),
        (MAR, 2, 1, 2),
        (APR, 2, None, None),
        (date(2024, 5, 1), 2, None, None),
    ]
    as_of = QueryObject(select={"dimensions": [], "measures": COUNTS}, asOf="2024-02-01")
    compiled = CompilationPipeline().compile(as_of, model, "duckdb")
    assert con.execute(compiled.sql).fetchall() == [(1, None, 1)]


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
            {
                "select": {
                    "dimensions": [{"coalesce": ["Order Month", "Order Day"], "as": "When"}],
                    "measures": ["Revenue"],
                }
            },
            "FILL_REQUIRES_ONE_TIME_DIMENSION",
        ),
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
            {"select": {"dimensions": ["Order Day:month"], "measures": ["Running Revenue"]}},
            "FILL_COMBINATION_NOT_SUPPORTED",
        ),
        (
            {"select": {"dimensions": ["Order Month"], "measures": ["Running Average Per Order"]}},
            "FILL_COMBINATION_NOT_SUPPORTED",
        ),
    ],
    ids=[
        "no-time",
        "two-times",
        "raw",
        "coalesced-time",
        "hour",
        "rollup",
        "having",
        "total",
        "cumulative-other-time",
        "cumulative-over-reaggregate",
    ],
)
def test_refused(model: SemanticModel, query: dict, code: str) -> None:
    with pytest.raises(ResolutionError) as raised:
        CompilationPipeline().compile(QueryObject(**query, fill=True), model, "duckdb")
    assert code in {error.code for error in raised.value.errors}
