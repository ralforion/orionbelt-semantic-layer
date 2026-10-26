"""Moving aggregates over a row frame (window metric ``frame``), run on DuckDB.

``windowFunction: sum | avg | min | max | count`` with ``frame: {preceding: N}``
is the base measure over the current row and the N rows before it, in the
query's result ordered by time (``compiler/window_wrap.py``). Every case is
checked against the same window written straight over the grouped table.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError
from rdflib import Literal, URIRef

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import Metric, SemanticModel
from orionbelt.obsl.exporter import OBSL, export_obsl
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

FUNCTIONS = ["sum", "avg", "min", "max", "count"]

MODEL_YAML = """
version: "1.0"
name: window_frame
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
  Customer: {dataObject: Orders, column: Customer, resultType: string}
  Order Year: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: year}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
metrics:
<METRICS>
  Revenue Status MA:
    type: window
    windowFunction: avg
    measure: Revenue
    timeDimension: Order Date
    frame: {preceding: 1}
    partitionBy: [Status]
  Revenue Share of Moving Sum:
    expression: '{[Revenue]} / {[Revenue sum 3]}'
  Revenue per Customer:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: avg
  Per Customer Moving Max:
    type: window
    windowFunction: max
    measure: Revenue per Customer
    timeDimension: Order Date
    frame: {preceding: 1}
""".replace(
    "<METRICS>",
    "\n".join(
        f"  Revenue {f} 3:\n    type: window\n    windowFunction: {f}\n    measure: Revenue\n"
        "    timeDimension: Order Date\n    frame: {preceding: 2}"
        for f in FUNCTIONS
    ),
)

# No orders in February 2025: the frame skips it rather than counting it.
ORDERS = [
    ("o1", "c1", "ok", date(2024, 10, 5), 10.0),
    ("o2", "c2", "ok", date(2024, 11, 20), 20.0),
    ("o3", "c1", "late", date(2024, 12, 31), 40.0),
    ("o4", "c3", "ok", date(2025, 1, 15), 80.0),
    ("o5", "c1", "ok", date(2025, 1, 20), 5.0),
    ("o6", "c2", "late", date(2025, 3, 30), 160.0),
    ("o7", "c4", "ok", date(2025, 4, 2), 1.0),
    ("o8", "c4", "ok", None, 7.0),
]


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


def _run(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, **query: Any
) -> list[tuple[Any, ...]]:
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    return sorted(con.execute(sql).fetchall(), key=repr)


def _truth(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    return sorted(con.execute(sql).fetchall(), key=repr)


MONTHLY = (
    "WITH m AS (SELECT CAST(date_trunc('month', order_date) AS DATE) AS month,"
    " SUM(amount) AS rev FROM orders GROUP BY 1)"
)


@pytest.mark.parametrize("function", FUNCTIONS)
def test_each_function_over_the_months(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, function: str
) -> None:
    got = _run(
        con,
        model,
        select={"dimensions": ["Order Date:month"], "measures": [f"Revenue {function} 3"]},
    )
    assert got == _truth(
        con,
        f"{MONTHLY} SELECT month, {function}(rev) OVER (ORDER BY month NULLS LAST"
        " ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM m",
    )
    # March reads December, January and March: February has no row.
    march = next(row for row in got if row[0] == date(2025, 3, 1))
    expected = {"sum": 285.0, "avg": 95.0, "min": 40.0, "max": 160.0, "count": 3}
    assert march[1] == pytest.approx(expected[function])


def test_other_dimensions_partition_the_frame(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    got = _run(
        con,
        model,
        select={
            "dimensions": ["Order Date:month", "Status"],
            "measures": ["Revenue", "Revenue avg 3", "Revenue Status MA"],
        },
    )
    assert got == _truth(
        con,
        "WITH m AS (SELECT CAST(date_trunc('month', order_date) AS DATE) AS month, status,"
        " SUM(amount) AS rev FROM orders GROUP BY 1, 2)"
        " SELECT month, status, rev,"
        " AVG(rev) OVER (PARTITION BY status ORDER BY month NULLS LAST"
        " ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),"
        " AVG(rev) OVER (PARTITION BY status ORDER BY month NULLS LAST"
        " ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM m",
    )


def test_a_selected_partition_key_is_not_repeated(model: SemanticModel) -> None:
    sql = (
        CompilationPipeline()
        .compile(
            QueryObject(
                select={
                    "dimensions": ["Order Date:month", "Status"],
                    "measures": ["Revenue Status MA"],
                }
            ),
            model,
            "duckdb",
        )
        .sql
    )
    assert 'PARTITION BY "Status" ORDER BY' in sql


def test_a_year_beside_the_month_orders_the_rows(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    # The year is a position on the time axis: January's frame reaches into
    # the previous year's rows.
    got = _run(
        con,
        model,
        select={"dimensions": ["Order Year", "Order Date:month"], "measures": ["Revenue sum 3"]},
    )
    assert got == _truth(
        con,
        "WITH m AS (SELECT CAST(date_trunc('year', order_date) AS DATE) AS year,"
        " CAST(date_trunc('month', order_date) AS DATE) AS month, SUM(amount) AS rev"
        " FROM orders GROUP BY 1, 2)"
        " SELECT year, month, SUM(rev) OVER (ORDER BY month NULLS LAST, year NULLS LAST"
        " ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM m",
    )
    january = next(row for row in got if row[1] == date(2025, 1, 1))
    assert january[2] == pytest.approx(20.0 + 40.0 + 85.0)


def test_a_formula_over_a_frame_metric(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    got = _run(
        con,
        model,
        select={"dimensions": ["Order Date:month"], "measures": ["Revenue Share of Moving Sum"]},
    )
    truth = _truth(
        con,
        f"{MONTHLY} SELECT month, rev / SUM(rev) OVER (ORDER BY month NULLS LAST"
        " ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM m",
    )
    assert [r[0] for r in got] == [r[0] for r in truth]
    assert [float(r[1]) for r in got] == pytest.approx([r[1] for r in truth], abs=1e-6)


def test_over_a_reaggregate_metric(con: duckdb.DuckDBPyConnection, model: SemanticModel) -> None:
    got = _run(
        con,
        model,
        select={"dimensions": ["Order Date:month"], "measures": ["Per Customer Moving Max"]},
    )
    truth = _truth(
        con,
        "WITH c AS (SELECT CAST(date_trunc('month', order_date) AS DATE) AS month,"
        " customer_id, SUM(amount) AS rev FROM orders GROUP BY 1, 2),"
        " m AS (SELECT month, AVG(rev) AS v FROM c GROUP BY 1)"
        " SELECT month, MAX(v) OVER (ORDER BY month NULLS LAST"
        " ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM m",
    )
    assert [r[0] for r in got] == [r[0] for r in truth]
    assert [float(r[1]) for r in got] == pytest.approx([r[1] for r in truth])


def test_a_time_filter_clips_the_frame(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    got = _run(
        con,
        model,
        select={"dimensions": ["Order Date:month"], "measures": ["Revenue sum 3"]},
        where=[{"field": "Order Date", "op": ">=", "value": "2025-01-01"}],
    )
    assert got == [
        (date(2025, 1, 1), 85.0),
        (date(2025, 3, 1), 245.0),
        (date(2025, 4, 1), 246.0),
    ]


def test_having_on_a_frame_metric(con: duckdb.DuckDBPyConnection, model: SemanticModel) -> None:
    # The frame is computed over every row before HAVING drops any.
    got = _run(
        con,
        model,
        select={"dimensions": ["Order Date:month"], "measures": ["Revenue sum 3"]},
        having=[{"field": "Revenue sum 3", "op": ">", "value": 200}],
    )
    assert got == [(date(2025, 3, 1), 285.0), (date(2025, 4, 1), 246.0)]


def test_the_time_dimension_must_be_selected(model: SemanticModel) -> None:
    with pytest.raises(ResolutionError) as raised:
        CompilationPipeline().compile(
            QueryObject(select={"dimensions": ["Status"], "measures": ["Revenue avg 3"]}),
            model,
            "duckdb",
        )
    assert raised.value.errors[0].code == "WINDOW_TIME_DIMENSION_NOT_IN_SELECT"


def test_rollup_is_refused(model: SemanticModel) -> None:
    with pytest.raises(ResolutionError) as raised:
        CompilationPipeline().compile(
            QueryObject(
                select={
                    "dimensions": ["Order Date:month", "Status"],
                    "measures": ["Revenue avg 3"],
                },
                grouping="rollup",
            ),
            model,
            "duckdb",
        )
    assert raised.value.errors[0].code == "WINDOW_FRAME_NOT_SUPPORTED"


_BASE = {
    "name": "M",
    "type": "window",
    "measure": "Revenue",
    "timeDimension": "Order Date",
}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"windowFunction": "avg"}, "requires 'frame'"),
        ({"windowFunction": "rank", "frame": {"preceding": 2}}, "'frame' applies to"),
        ({"windowFunction": "avg", "frame": {"preceding": 0}}, "must be >= 1"),
        ({"windowFunction": "avg", "frame": {"preceding": 2}, "offset": 1}, "does not take offset"),
        (
            {"windowFunction": "sum", "frame": {"preceding": 2}, "orderDirection": "desc"},
            "does not take orderDirection",
        ),
        (
            {"windowFunction": "count", "frame": {"preceding": 2}, "timeDimension": None},
            "requires 'timeDimension'",
        ),
    ],
)
def test_metric_validation(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Metric.model_validate({**_BASE, **fields})


def test_frame_on_a_cumulative_metric_is_refused_at_load() -> None:
    yaml_text = MODEL_YAML + (
        "  Bad:\n    type: cumulative\n    measure: Revenue\n"
        "    timeDimension: Order Date\n    frame: {preceding: 2}\n"
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    _, result = ReferenceResolver().resolve(raw, source_map)
    assert not result.valid
    assert any("'frame' is only valid on window metrics" in e.message for e in result.errors)


def test_order_direction_default_still_loads_for_ranking(model: SemanticModel) -> None:
    raw, source_map = TrackedLoader().load_string(
        MODEL_YAML + "  Rank:\n    type: window\n    windowFunction: rank\n    measure: Revenue\n"
    )
    loaded, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert loaded.metrics["Rank"].order_direction == "desc"


def test_the_graph_carries_the_frame(model: SemanticModel) -> None:
    graph = export_obsl(model, "window_frame")
    values = {
        (str(s), o.toPython())
        for s, o in graph.subject_objects(OBSL.windowFramePreceding)
        if isinstance(s, URIRef) and isinstance(o, Literal)
    }
    assert len(values) == len(FUNCTIONS) + 2
    assert {v for _, v in values} == {1, 2}
