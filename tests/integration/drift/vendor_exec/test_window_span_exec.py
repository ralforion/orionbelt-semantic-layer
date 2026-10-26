"""Rolling windows over a span of time (``windowSpan``) on every engine.

The span joins each shown period to the measure's rows by a ``date_add`` range
(``compiler/span_window.py``); every engine renders that range and rebuilds the
measure's aggregation over the joined rows. The expected values are computed
here in Python from the same rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create

pytestmark = pytest.mark.docker

ORDERS = "ws_orders"

ROWS = [
    ("c1", "ok", 10.0, "2024-10-05"),
    ("c2", "ok", 20.0, "2024-11-20"),
    ("c1", "late", 40.0, "2024-12-31"),
    ("c3", "ok", 80.0, "2025-01-15"),
    ("c1", "ok", 5.0, "2025-02-01"),
    ("c2", "late", 160.0, "2025-03-30"),
    ("c4", "ok", 1.0, "2025-04-02"),
]

MODEL_YAML = """
version: "1.0"
name: window_span_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Customer: {{code: customer_id, abstractType: string}}
      Status: {{code: status, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
      Order Date: {{code: order_date, abstractType: date}}
dimensions:
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
  Status: {{dataObject: Orders, column: Status, resultType: string}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Average: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: avg}}
  Customers:
    columns: [{{dataObject: Orders, column: Customer}}]
    aggregation: count_distinct
    resultType: int
metrics:
  Revenue 90d:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    windowSpan: {{count: 90, unit: day}}
  Average 90d:
    type: cumulative
    measure: Average
    timeDimension: Order Date
    windowSpan: {{count: 90, unit: day}}
  Customers 90d:
    type: cumulative
    measure: Customers
    timeDimension: Order Date
    windowSpan: {{count: 90, unit: day}}
"""


def _month_end(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def _expected(by_status: bool) -> dict[tuple, tuple[float, float, int]]:
    """Per shown (month[, status]): the 90-day sum, average and distinct count."""
    rows = [(c, s, a, date.fromisoformat(d)) for c, s, a, d in ROWS]
    shown = {(d.replace(day=1), s if by_status else None) for _, s, _, d in rows}
    expected = {}
    for month, status in shown:
        end = _month_end(month)
        inside = [
            (c, a)
            for c, s, a, d in rows
            if end - timedelta(days=90) <= d < end and (status is None or s == status)
        ]
        key = (month, status) if by_status else (month,)
        expected[key] = (
            sum(a for _, a in inside),
            fmean(a for _, a in inside),
            len({c for c, _ in inside}),
        )
    return expected


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(
        target,
        ORDERS,
        [("customer_id", 0), ("status", 0), ("amount", 1), ("order_date", 3)],
        ROWS,
    )
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _run(target: VendorTarget, model: SemanticModel, dimensions: list[str]) -> dict:
    query = QueryObject(
        select={
            "dimensions": dimensions,
            "measures": ["Revenue 90d", "Average 90d", "Customers 90d"],
        }
    )
    sql = CompilationPipeline().compile(query, model, target.dialect).sql
    got = {}
    for row in target.execute(sql):
        values = list(row.values())
        key = (date.fromisoformat(str(values[0])[:10]), *values[1 : len(dimensions)])
        revenue, average, customers = values[len(dimensions) :]
        got[key] = (float(revenue), float(average), int(customers))
    return got


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    for dimensions, by_status in (
        (["Order Date:month"], False),
        (["Order Date:month", "Status"], True),
    ):
        got = _run(target, model, dimensions)
        expected = _expected(by_status)
        assert got.keys() == expected.keys()
        for key, (revenue, average, customers) in expected.items():
            assert got[key][0] == pytest.approx(revenue), key
            assert got[key][1] == pytest.approx(average), key
            assert got[key][2] == customers, key


def test_duckdb_window_span(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_window_span(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_window_span(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_window_span(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_window_span(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_window_span(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_window_span(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_window_span(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
