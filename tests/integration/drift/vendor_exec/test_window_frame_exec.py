"""Moving aggregates over a row frame (window metric ``frame``) on every engine.

Each function is the base measure over the current row and the rows before it,
``ROWS BETWEEN N PRECEDING AND CURRENT ROW`` in the result ordered by time
(``compiler/window_wrap.py``). The expected values are computed here in Python
from the same rows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create

pytestmark = pytest.mark.docker

ORDERS = "wf_orders"
FUNCTIONS = ["sum", "avg", "min", "max", "count"]

# No orders in February 2025: the frame skips it.
ROWS = [
    ("ok", 10.0, "2024-10-05"),
    ("ok", 20.0, "2024-11-20"),
    ("late", 40.0, "2024-12-31"),
    ("ok", 80.0, "2025-01-15"),
    ("ok", 5.0, "2025-01-20"),
    ("late", 160.0, "2025-03-30"),
    ("ok", 1.0, "2025-04-02"),
]

MODEL_YAML = """
version: "1.0"
name: window_frame_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Status: {{code: status, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
      Order Date: {{code: order_date, abstractType: date}}
dimensions:
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
  Status: {{dataObject: Orders, column: Status, resultType: string}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
metrics:
<METRICS>
""".replace(
    "<METRICS>",
    "\n".join(
        f"  Revenue {f} 3:\n    type: window\n    windowFunction: {f}\n    measure: Revenue\n"
        "    timeDimension: Order Date\n    frame: {{preceding: 2}}"
        for f in FUNCTIONS
    ),
)

_AGGREGATE = {
    "sum": sum,
    "avg": lambda values: sum(values) / len(values),
    "min": min,
    "max": max,
    "count": len,
}


def _expected(by_status: bool) -> dict[tuple, list[float]]:
    """Per (month[, status]): each function over that row and the two before it."""
    monthly: dict[tuple, float] = defaultdict(float)
    for status, amount, day in ROWS:
        month = date.fromisoformat(day).replace(day=1)
        monthly[(month, status) if by_status else (month,)] += amount
    partitions: dict[object, list[tuple]] = defaultdict(list)
    for key in sorted(monthly):
        partitions[key[1] if by_status else None].append(key)
    expected = {}
    for keys in partitions.values():
        for i, key in enumerate(keys):
            frame = [monthly[k] for k in keys[max(0, i - 2) : i + 1]]
            expected[key] = [_AGGREGATE[f](frame) for f in FUNCTIONS]
    return expected


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, ORDERS, [("status", 0), ("amount", 1), ("order_date", 3)], ROWS)
    yaml_text = MODEL_YAML.format(orders=ORDERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    for dimensions, by_status in (
        (["Order Date:month"], False),
        (["Order Date:month", "Status"], True),
    ):
        query = QueryObject(
            select={
                "dimensions": dimensions,
                "measures": [f"Revenue {f} 3" for f in FUNCTIONS],
            }
        )
        sql = CompilationPipeline().compile(query, model, target.dialect).sql
        got = {}
        for row in target.execute(sql):
            values = list(row.values())
            key = (date.fromisoformat(str(values[0])[:10]), *values[1 : len(dimensions)])
            got[key] = [float(v) for v in values[len(dimensions) :]]
        expected = _expected(by_status)
        assert got.keys() == expected.keys()
        for key, values in expected.items():
            assert got[key] == pytest.approx(values), key


def test_duckdb_window_frame(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_window_frame(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_window_frame(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_window_frame(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_window_frame(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_window_frame(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_window_frame(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_window_frame(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
