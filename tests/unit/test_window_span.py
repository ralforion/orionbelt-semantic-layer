"""Rolling windows over a span of time (``windowSpan``), compiled and run on DuckDB.

For each shown period, the metric is its measure over the source rows in the
span ending at that period's end (``compiler/span_window.py``). Every case is
checked against a correlated subquery written straight against the table.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MEASURES = {
    "Revenue": "aggregation: sum",
    "Orders Placed": "aggregation: count",
    "Smallest": "aggregation: min",
    "Largest": "aggregation: max",
    "Average": "aggregation: avg",
}

MODEL_YAML = """
version: "1.0"
name: window_span
dataObjects:
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer_id, abstractType: string}
      Status: {code: status, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
dimensions:
  Order Date: {dataObject: Orders, column: Order Date, resultType: date}
  Status: {dataObject: Orders, column: Status, resultType: string}
  Order Year: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: year}
measures:
<MEASURES>
  Customers:
    columns: [{dataObject: Orders, column: Customer}]
    aggregation: count_distinct
    resultType: int
  Latest:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: last
    withinGroup: {column: {dataObject: Orders, column: Order Date}}
    resultType: float
  Earliest:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: first
    withinGroup: {column: {dataObject: Orders, column: Order Date}}
    resultType: float
metrics:
<METRICS>
  Revenue 3 Months:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    window: 3
"""
MODEL_YAML = MODEL_YAML.replace(
    "<MEASURES>",
    "\n".join(
        f"  {name}:\n    columns: [{{dataObject: Orders, column: Amount}}]\n    {agg}\n"
        "    resultType: float"
        for name, agg in MEASURES.items()
    ),
).replace(
    "<METRICS>",
    "\n".join(
        f"  {name} 90d:\n    type: cumulative\n    measure: {name}\n"
        "    timeDimension: Order Date\n    windowSpan: {count: 90, unit: day}"
        for name in [*MEASURES, "Customers", "Latest", "Earliest"]
    ),
)

ORDERS = [
    ("o1", "c1", "ok", date(2024, 10, 5), 10.0),
    ("o2", "c2", "ok", date(2024, 11, 20), 20.0),
    ("o3", "c1", "late", date(2024, 12, 31), 40.0),
    ("o4", "c3", "ok", date(2025, 1, 15), 80.0),
    ("o5", "c1", "ok", date(2025, 2, 1), 5.0),
    ("o6", "c2", "late", date(2025, 3, 30), 160.0),
    ("o7", "c4", "ok", date(2025, 4, 2), 1.0),
]

# The window for month m: the 90 days up to the end of m.
WINDOW = (
    "o.order_date >= m + INTERVAL 1 MONTH - INTERVAL 90 DAY AND o.order_date < m + INTERVAL 1 MONTH"
)


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (id VARCHAR, customer_id VARCHAR, status VARCHAR,"
        " order_date DATE, amount DOUBLE)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", ORDERS)
    return con


def _rows(model: SemanticModel, con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple]:
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return _normal(con.execute(sql).fetchall())


def _normal(rows: list[Any]) -> list[tuple]:
    def cell(value: Any) -> Any:
        return round(float(value), 6) if isinstance(value, float | int) else value

    return sorted((tuple(cell(v) for v in row) for row in rows), key=str)


def _truth(con: duckdb.DuckDBPyConnection, aggregate: str, months: str = "") -> list[tuple]:
    sql = (
        f"SELECT m, ({aggregate}) FROM (SELECT DISTINCT date_trunc('month', order_date)::DATE m"
        f" FROM orders {months})"
    )
    return _normal(con.execute(sql).fetchall())


@pytest.mark.parametrize(
    ("metric", "aggregate"),
    [
        ("Revenue 90d", f"SELECT sum(amount) FROM orders o WHERE {WINDOW}"),
        ("Orders Placed 90d", f"SELECT count(*) FROM orders o WHERE {WINDOW}"),
        ("Smallest 90d", f"SELECT min(amount) FROM orders o WHERE {WINDOW}"),
        ("Largest 90d", f"SELECT max(amount) FROM orders o WHERE {WINDOW}"),
        ("Average 90d", f"SELECT avg(amount) FROM orders o WHERE {WINDOW}"),
        ("Customers 90d", f"SELECT count(DISTINCT customer_id) FROM orders o WHERE {WINDOW}"),
        (
            "Latest 90d",
            f"SELECT amount FROM orders o WHERE {WINDOW} ORDER BY order_date DESC LIMIT 1",
        ),
        (
            "Earliest 90d",
            f"SELECT amount FROM orders o WHERE {WINDOW} ORDER BY order_date ASC LIMIT 1",
        ),
    ],
)
def test_every_aggregation_over_the_span(
    model: SemanticModel, con: duckdb.DuckDBPyConnection, metric: str, aggregate: str
) -> None:
    query = {"select": {"dimensions": ["Order Date:month"], "measures": [metric]}}
    assert _rows(model, con, query) == _truth(con, aggregate)


def test_a_distinct_count_counts_a_customer_once_per_span(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {"select": {"dimensions": ["Order Date:month"], "measures": ["Customers 90d"]}}
    by_month = dict(_rows(model, con, query))
    # January's 90 days hold c1 twice (Oct and Dec) and c2 and c3 once: 3, not 4.
    assert by_month[date(2025, 1, 1)] == 3


def test_the_time_filter_picks_the_periods_not_the_rows(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue 90d"]},
        "where": [{"field": "Order Date", "op": "between", "value": ["2025-01-01", "2025-03-31"]}],
    }
    # January still reads November and December.
    assert _rows(model, con, query) == _truth(
        con,
        f"SELECT sum(amount) FROM orders o WHERE {WINDOW}",
        "WHERE order_date BETWEEN '2025-01-01' AND '2025-03-31'",
    )


def test_another_filter_clips_the_rows(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue 90d"]},
        "where": [{"field": "Status", "op": "=", "value": "ok"}],
    }
    assert _rows(model, con, query) == _truth(
        con,
        f"SELECT sum(amount) FROM orders o WHERE status = 'ok' AND {WINDOW}",
        "WHERE status = 'ok'",
    )


def test_each_group_has_its_own_window(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {"select": {"dimensions": ["Order Date:month", "Status"], "measures": ["Revenue 90d"]}}
    truth = _normal(
        con.execute(
            "SELECT m, s, (SELECT sum(amount) FROM orders o WHERE o.status = s AND "
            f"{WINDOW}) FROM (SELECT DISTINCT date_trunc('month', order_date)::DATE m,"
            " status s FROM orders)"
        ).fetchall()
    )
    assert _rows(model, con, query) == truth


def test_beside_a_plain_measure_and_a_period_window(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Revenue", "Revenue 90d", "Revenue 3 Months"],
        }
    }
    rows = {r[0]: r[1:] for r in _rows(model, con, query)}
    # March: 160 in the month, 245 in its 90 days (Jan 15 on), 245 in Jan-Mar.
    assert rows[date(2025, 3, 1)] == (160.0, 245.0, 245.0)
    # April: the 90 days start on Jan 31, a day past January's order.
    assert rows[date(2025, 4, 1)] == (1.0, 166.0, 166.0)


def test_a_week_period_with_a_day_span(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    raw, source_map = TrackedLoader().load_string(
        MODEL_YAML.replace(
            "windowSpan: {count: 90, unit: day}", "windowSpan: {count: 14, unit: day}"
        )
    )
    weekly, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid
    query = {"select": {"dimensions": ["Order Date:week"], "measures": ["Revenue 90d"]}}
    sql = CompilationPipeline().compile(QueryObject(**query), weekly, "duckdb").sql
    got = _normal(con.execute(sql).fetchall())
    truth = _normal(
        con.execute(
            "SELECT w, (SELECT sum(amount) FROM orders o WHERE o.order_date >= w + INTERVAL 7 DAY"
            " - INTERVAL 14 DAY AND o.order_date < w + INTERVAL 7 DAY) FROM (SELECT DISTINCT"
            " date_trunc('week', order_date)::DATE w FROM orders)"
        ).fetchall()
    )
    assert got == truth


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({"select": {"measures": ["Revenue 90d"]}}, "WINDOW_SPAN_NOT_SUPPORTED"),
        (
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue 90d"]},
                "grouping": "rollup",
            },
            "WINDOW_SPAN_NOT_SUPPORTED",
        ),
    ],
    ids=["time-dimension-not-selected", "rollup"],
)
def test_refusals(model: SemanticModel, query: dict, code: str) -> None:
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(QueryObject(**query), model, "duckdb")
    assert code in [e.code for e in excinfo.value.errors]


@pytest.mark.parametrize(
    "fields",
    [
        "windowSpan: {count: 90, unit: day}\n    window: 3",
        "windowSpan: {count: 90, unit: day}\n    cumulativeType: avg",
        "windowSpan: {count: 90, unit: day}\n    partitionBy: [Status]",
        "windowSpan: {count: 0, unit: day}",
    ],
    ids=["with-window", "with-cumulative-type", "with-partition", "zero"],
)
def test_invalid_spans_are_refused_at_load(fields: str) -> None:
    yaml_text = (
        MODEL_YAML
        + f"""
  Bad:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    {fields}
"""
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    _, result = ReferenceResolver().resolve(raw, source_map)
    assert not result.valid


def test_a_span_is_cumulative_only() -> None:
    yaml_text = (
        MODEL_YAML
        + """
  Bad:
    type: derived
    expression: '{[Revenue]}'
    windowSpan: {count: 90, unit: day}
"""
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    _, result = ReferenceResolver().resolve(raw, source_map)
    assert not result.valid


def test_the_span_is_an_xsd_duration_in_the_graph(model: SemanticModel) -> None:
    from rdflib import Literal
    from rdflib.namespace import XSD

    from orionbelt.obsl.exporter import OBSL, export_obsl

    spans = set(export_obsl(model, "window_span").objects(predicate=OBSL.windowSpan))
    assert spans == {Literal("P90D", datatype=XSD.duration)}


@pytest.mark.parametrize(("dialect", "keyed"), [("clickhouse", True), ("duckdb", False)])
def test_clickhouse_gets_a_key_for_the_range_join(
    model: SemanticModel, dialect: str, keyed: bool
) -> None:
    query = {"select": {"dimensions": ["Order Date:month"], "measures": ["Revenue 90d"]}}
    sql = CompilationPipeline().compile(QueryObject(**query), model, dialect).sql
    # ClickHouse refuses an outer join on inequalities alone over an aggregate.
    assert ("IS NOT NULL) = (" in sql) is keyed


def test_a_year_beside_the_month_does_not_split_the_window(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {
            "dimensions": ["Order Year", "Order Date:month"],
            "measures": ["Revenue 90d"],
        }
    }
    by_month = {r[1]: r[2] for r in _rows(model, con, query)}
    # January 2025's 90 days reach back into 2024: 20 + 40 + 80.
    assert by_month[date(2025, 1, 1)] == 140.0


def test_a_filled_period_reads_its_span(model: SemanticModel) -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (id VARCHAR, customer_id VARCHAR, status VARCHAR,"
        " order_date DATE, amount DOUBLE)"
    )
    # No order in February: fill adds the month, and its 90 days still hold
    # December's and January's orders.
    con.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [o for o in ORDERS if o[0] != "o5"]
    )
    query = {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue 90d"]},
        "fill": True,
    }
    by_month = dict(_rows(model, con, query))
    assert by_month[date(2025, 2, 1)] == 120.0
    assert by_month[date(2025, 3, 1)] == 240.0


def test_a_span_over_a_reaggregate_metric_is_refused_at_load() -> None:
    yaml_text = (
        MODEL_YAML
        + """
  Avg Per Order:
    type: reaggregate
    measure: Revenue
    per: [Status]
    aggregation: avg
  Bad:
    type: cumulative
    measure: Avg Per Order
    timeDimension: Order Date
    windowSpan: {count: 90, unit: day}
"""
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    _, result = ReferenceResolver().resolve(raw, source_map)
    assert "WINDOW_SPAN_NOT_SUPPORTED" in [e.code for e in result.errors]


def test_a_span_over_a_filter_context_measure_is_refused() -> None:
    yaml_text = MODEL_YAML.replace(
        "  Revenue:\n    columns: [{dataObject: Orders, column: Amount}]\n    aggregation: sum\n",
        "  Revenue:\n    columns: [{dataObject: Orders, column: Amount}]\n    aggregation: sum\n"
        "    filterContext: {mode: FIXED}\n",
    )
    assert "filterContext" in yaml_text
    raw, source_map = TrackedLoader().load_string(yaml_text)
    fixed, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = {"select": {"dimensions": ["Order Date:month"], "measures": ["Revenue 90d"]}}
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(QueryObject(**query), fixed, "duckdb")
    assert [e.code for e in excinfo.value.errors] == ["WINDOW_SPAN_NOT_SUPPORTED"]
