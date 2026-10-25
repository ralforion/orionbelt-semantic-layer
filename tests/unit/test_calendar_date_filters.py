"""Calendar-aligned relative filters and period literals.

``relative`` with ``calendar: true`` takes whole calendar periods (this
quarter, the last full month, year to date) instead of counting back from
today; a period literal (``'2025-Q1'``, ``'2024-12'``, ``'2025'``) in a date
filter value stands for its bounds.

The relative cases run on DuckDB against one order on the 15th of every month
around today, plus one today, so they hold whatever day they run; the expected
rows are computed in Python from the same calendar.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from orionbelt.compiler.period_literals import period_bounds
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: calendar_date_filters
dataObjects:
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: int, primaryKey: true}
      Order Date: {code: order_date, abstractType: date}
      Ordered At: {code: ordered_at, abstractType: timestamp}
      Batch: {code: batch, abstractType: string}
dimensions:
  Order Date: {dataObject: Orders, column: Order Date, resultType: date}
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
  Batch: {dataObject: Orders, column: Batch, resultType: string}
"""

TODAY = date.today()


def _month_start(day: date, shift: int = 0) -> date:
    months = day.year * 12 + day.month - 1 + shift
    return date(months // 12, months % 12 + 1, 1)


def _quarter_start(day: date, shift: int = 0) -> date:
    return _month_start(date(day.year, 3 * ((day.month - 1) // 3) + 1, 1), 3 * shift)


# One order on the 15th of each month, three years back to a year ahead, and today's.
RELATIVE_DAYS = sorted(
    {_month_start(TODAY, shift).replace(day=15) for shift in range(-36, 13)} | {TODAY}
)


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _con(days: list[date]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (id INT, order_date DATE, ordered_at TIMESTAMP, batch VARCHAR)"
    )
    con.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?)",
        [(i, day, f"{day} 23:00:00", str(day.year)) for i, day in enumerate(days)],
    )
    return con


def _days(model: SemanticModel, con: duckdb.DuckDBPyConnection, *where: dict) -> list[date]:
    query = QueryObject(**{"select": {"dimensions": ["Order Date"]}, "where": list(where)})
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    return sorted(row[0] for row in con.execute(sql).fetchall())


def _relative(**value: object) -> dict:
    return {"field": "Order Date", "op": "relative", "value": value}


def _between(start: date, end: date) -> list[date]:
    """The relative days in ``[start, end)``."""
    return [day for day in RELATIVE_DAYS if start <= day < end]


@pytest.mark.parametrize(
    ("value", "start", "end"),
    [
        ({"unit": "quarter", "count": 1}, _quarter_start(TODAY), _quarter_start(TODAY, 1)),
        (
            {"unit": "month", "count": 1, "include_current": False},
            _month_start(TODAY, -1),
            _month_start(TODAY),
        ),
        (
            {"unit": "quarter", "count": 4, "include_current": False},
            _quarter_start(TODAY, -4),
            _quarter_start(TODAY),
        ),
        (
            {"unit": "month", "count": 3},
            _month_start(TODAY, -2),
            _month_start(TODAY, 1),
        ),
        (
            {"unit": "month", "count": 2, "direction": "future", "include_current": False},
            _month_start(TODAY, 1),
            _month_start(TODAY, 3),
        ),
        (
            {"unit": "year", "count": 1, "toDate": True},
            date(TODAY.year, 1, 1),
            TODAY + timedelta(days=1),
        ),
    ],
    ids=[
        "this-quarter",
        "last-full-month",
        "last-4-full-quarters",
        "last-3-months-with-current",
        "next-2-full-months",
        "year-to-date",
    ],
)
def test_calendar_relative(model: SemanticModel, value: dict, start: date, end: date) -> None:
    con = _con(RELATIVE_DAYS)
    assert _days(model, con, _relative(calendar=True, **value)) == _between(start, end)


def test_without_calendar_the_window_still_rolls(model: SemanticModel) -> None:
    con = _con(RELATIVE_DAYS)
    rolled = _days(model, con, _relative(unit="day", count=40))
    assert rolled == _between(TODAY - timedelta(days=39), TODAY + timedelta(days=1))


@pytest.mark.parametrize(
    "value",
    [
        {"unit": "year", "count": 1, "toDate": True},
        {"unit": "year", "count": 1, "calendar": True, "toDate": True, "include_current": False},
        {"unit": "year", "count": 1, "calendar": True, "toDate": True, "direction": "future"},
        {"unit": "year", "count": 1, "calendar": "yes"},
        {"unit": "fortnight", "count": 1, "calendar": True},
    ],
    ids=["to-date-rolling", "to-date-past-periods", "to-date-future", "non-bool", "bad-unit"],
)
def test_invalid_relative_filters_are_refused(model: SemanticModel, value: dict) -> None:
    query = QueryObject(**{"select": {"dimensions": ["Order Date"]}, "where": [_relative(**value)]})
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(query, model, "duckdb")
    assert [e.code for e in excinfo.value.errors] == ["INVALID_RELATIVE_FILTER"]


@pytest.mark.parametrize(
    ("literal", "bounds"),
    [
        ("2025", (date(2025, 1, 1), date(2026, 1, 1))),
        ("2025-Q1", (date(2025, 1, 1), date(2025, 4, 1))),
        ("2025-q4", (date(2025, 10, 1), date(2026, 1, 1))),
        ("2024-12", (date(2024, 12, 1), date(2025, 1, 1))),
        ("2025-02", (date(2025, 2, 1), date(2025, 3, 1))),
        ("2025-01-15", None),
        ("2025-13", None),
        ("2025-Q5", None),
        (2025, None),
    ],
)
def test_period_bounds(literal: object, bounds: tuple[date, date] | None) -> None:
    assert period_bounds(literal) == bounds


LITERAL_DAYS = [
    date(2024, 11, 30),
    date(2024, 12, 31),
    date(2025, 1, 1),
    date(2025, 3, 31),
    date(2025, 4, 1),
    date(2025, 7, 4),
]


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("=", "2025-Q1", [date(2025, 1, 1), date(2025, 3, 31)]),
        (
            "!=",
            "2025-Q1",
            [date(2024, 11, 30), date(2024, 12, 31), date(2025, 4, 1), date(2025, 7, 4)],
        ),
        ("<=", "2024-12", [date(2024, 11, 30), date(2024, 12, 31)]),
        ("<", "2024-12", [date(2024, 11, 30)]),
        (">", "2025-Q1", [date(2025, 4, 1), date(2025, 7, 4)]),
        (">=", "2025", [date(2025, 1, 1), date(2025, 3, 31), date(2025, 4, 1), date(2025, 7, 4)]),
        ("between", ["2025-01", "2025-03"], [date(2025, 1, 1), date(2025, 3, 31)]),
        ("between", ["2024-12-31", "2025-01"], [date(2024, 12, 31), date(2025, 1, 1)]),
        (
            "notbetween",
            ["2024-12", "2025-Q1"],
            [date(2024, 11, 30), date(2025, 4, 1), date(2025, 7, 4)],
        ),
        ("in", ["2025-Q1", "2025-07-04"], [date(2025, 1, 1), date(2025, 3, 31), date(2025, 7, 4)]),
        ("not_in", ["2024", "2025-Q2"], [date(2025, 1, 1), date(2025, 3, 31), date(2025, 7, 4)]),
    ],
)
def test_period_literals(
    model: SemanticModel, op: str, value: object, expected: list[date]
) -> None:
    con = _con(LITERAL_DAYS)
    assert _days(model, con, {"field": "Order Date", "op": op, "value": value}) == expected


def test_a_timestamp_reads_the_whole_last_day(model: SemanticModel) -> None:
    # Each order is at 23:00: '<= 2024-12' keeps 31 December's, which a
    # comparison with the date '2024-12-31' would have left out.
    con = _con(LITERAL_DAYS)
    where = {"field": "Orders.Ordered At", "op": "<=", "value": "2024-12"}
    assert _days(model, con, where) == [date(2024, 11, 30), date(2024, 12, 31)]


def test_a_grained_dimension_and_another_filter(model: SemanticModel) -> None:
    con = _con(LITERAL_DAYS)
    days = _days(
        model,
        con,
        {"field": "Order Month", "op": "in", "value": ["2025-Q1", "2024-11"]},
        {"field": "Batch", "op": "=", "value": "2025"},
    )
    # The OR of the IN stays inside its own parentheses beside the AND.
    assert days == [date(2025, 1, 1), date(2025, 3, 31)]


def test_a_string_field_keeps_the_value(model: SemanticModel) -> None:
    con = _con(LITERAL_DAYS)
    assert _days(model, con, {"field": "Batch", "op": "=", "value": "2024"}) == [
        date(2024, 11, 30),
        date(2024, 12, 31),
    ]


def test_an_operator_without_a_period_reading_is_refused(model: SemanticModel) -> None:
    query = QueryObject(
        **{
            "select": {"dimensions": ["Order Date"]},
            "where": [{"field": "Order Date", "op": "contains", "value": "2025-Q1"}],
        }
    )
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(query, model, "duckdb")
    assert [e.code for e in excinfo.value.errors] == ["INVALID_FILTER_VALUE"]


SHIPMENTS = """
  Shipments:
    code: shipments
    columns:
      Shipment Id: {code: id, abstractType: int, primaryKey: true}
      Shipped Order: {code: order_id, abstractType: int}
      Ship Date: {code: ship_date, abstractType: date}
    joins:
      - {joinType: many-to-one, joinTo: Orders, columnsFrom: [Shipped Order], columnsTo: [Order Id]}
dimensions:"""

MEASURES = """measures:
  Order Rows:
    columns: [{dataObject: Orders, column: Order Id}]
    aggregation: count
    resultType: int
  Q1 Order Rows:
    columns: [{dataObject: Orders, column: Order Id}]
    aggregation: count
    resultType: int
    filterContext:
      mode: FIXED
      include:
        - {field: Order Date, op: '=', value: '2025-Q1'}
"""

PATHS_YAML = MODEL_YAML.replace("\ndimensions:", SHIPMENTS, 1) + MEASURES


def _paths_model(extra: str = "") -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(PATHS_YAML + extra)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture
def con_shipments() -> duckdb.DuckDBPyConnection:
    con = _con(LITERAL_DAYS)
    # Order 2 (1 January) ships in February, order 4 (1 April) in April.
    con.execute(
        "CREATE TABLE shipments AS SELECT * FROM (VALUES"
        " (1, 2, DATE '2025-02-10'), (2, 4, DATE '2025-04-05')) t(id, order_id, ship_date)"
    )
    return con


def _scalar(model: SemanticModel, con: duckdb.DuckDBPyConnection, query: dict) -> object:
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return con.execute(sql).fetchall()


def test_a_period_inside_an_exists_subquery(con_shipments: duckdb.DuckDBPyConnection) -> None:
    where = {
        "field": "Orders.Order Id",
        "op": "exists",
        "subquery": {
            "dataObject": "Shipments",
            "filter": [{"field": "Shipments.Ship Date", "op": "=", "value": "2025-Q1"}],
        },
    }
    assert _days(_paths_model(), con_shipments, where) == [date(2025, 1, 1)]


def test_a_period_in_a_filter_context_include(con_shipments: duckdb.DuckDBPyConnection) -> None:
    query = {"select": {"measures": ["Q1 Order Rows"]}}
    assert _scalar(_paths_model(), con_shipments, query) == [(2,)]


def test_a_period_in_a_static_model_filter(con_shipments: duckdb.DuckDBPyConnection) -> None:
    static = """filters:
  - dataObject: Orders
    column: Order Date
    operator: gte
    value: '2025'
"""
    query = {"select": {"measures": ["Order Rows"]}}
    assert _scalar(_paths_model(static), con_shipments, query) == [(4,)]


@pytest.mark.parametrize("literal", ["0000", "9999", "9999-12", "0000-Q1"])
def test_a_period_a_date_cannot_bound_is_refused(model: SemanticModel, literal: str) -> None:
    query = QueryObject(
        **{
            "select": {"dimensions": ["Order Date"]},
            "where": [{"field": "Order Date", "op": "=", "value": literal}],
        }
    )
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(query, model, "duckdb")
    assert [e.code for e in excinfo.value.errors] == ["INVALID_FILTER_VALUE"]


def test_the_last_bounded_period_compiles(model: SemanticModel) -> None:
    query = QueryObject(
        **{
            "select": {"dimensions": ["Order Date"]},
            "where": [{"field": "Order Date", "op": "=", "value": "9999-11"}],
        }
    )
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    assert "'9999-12-01'" in sql
