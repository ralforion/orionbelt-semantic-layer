"""A count is never NULL, wherever it is joined back onto the query's rows.

=========  ========  ========  =============  ======  ======
order      region    customer  date           status  amount
=========  ========  ========  =============  ======  ======
``o1``     r1        c1        2024-01-05     open    1
``o2``     r2        c2        2024-03-09     closed  2
``o3``     r1        c1        *(none)*       closed  4
``o4``     *(none)*  c2        2024-03-01     open    8
=========  ========  ========  =============  ======  ======

Region r3 has a return (5) and no orders; customer c3 has no orders at all.

Each case read NULL before: a ``filterContext`` count for a region without
open orders, and the NULL region's real count, lost to a ``=`` join; a
reaggregate count on r3, which only the returns bring; an ``asOf`` count and a
running sum of a count over no period, with and without ``fill``; and a
cumulative metric on the rows ``population: dimension`` adds, per period or
evaluated as of one. A count declared as text did not compile; it reads '0'.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _run

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: count_never_null
dataObjects:
  Regions:
    code: count_null_regions
    schema: '{schema}'
    columns:
      Region Id: {{code: id, abstractType: string, primaryKey: true}}
  Customers:
    code: count_null_customers
    schema: '{schema}'
    columns:
      Customer Id: {{code: id, abstractType: string, primaryKey: true}}
  Orders:
    code: count_null_orders
    schema: '{schema}'
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Region: {{code: region_id, abstractType: string}}
      Order Customer: {{code: customer_id, abstractType: string}}
      Order Date: {{code: order_date, abstractType: date}}
      Status: {{code: status, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Order Region]
        columnsTo: [Region Id]
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer Id]
  Returns:
    code: count_null_returns
    schema: '{schema}'
    columns:
      Return Id: {{code: id, abstractType: string, primaryKey: true}}
      Return Region: {{code: region_id, abstractType: string}}
      Refund: {{code: refund, abstractType: float}}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Return Region]
        columnsTo: [Region Id]
dimensions:
  Region: {{dataObject: Regions, column: Region Id, resultType: string}}
  Customer: {{dataObject: Customers, column: Customer Id, resultType: string}}
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
  Status: {{dataObject: Orders, column: Status, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    resultType: float
  Refunds:
    columns: [{{dataObject: Returns, column: Refund}}]
    aggregation: sum
    resultType: float
  Open Orders:
    columns: [{{dataObject: Orders, column: Order Id}}]
    aggregation: count
    resultType: int
    filterContext: {{mode: FIXED, include: [{{field: Status, op: '=', value: open}}]}}
  Open Customers:
    columns: [{{dataObject: Orders, column: Order Customer}}]
    aggregation: count_distinct
    resultType: int
    filterContext: {{mode: FIXED, include: [{{field: Status, op: '=', value: open}}]}}
  Lost Orders:
    columns: [{{dataObject: Orders, column: Order Id}}]
    aggregation: count
    resultType: int
    filterContext: {{mode: FIXED, include: [{{field: Status, op: '=', value: lost}}]}}
  Open Orders Text:
    columns: [{{dataObject: Orders, column: Order Id}}]
    aggregation: count
    resultType: string
    dataType: string
    filterContext: {{mode: FIXED, include: [{{field: Status, op: '=', value: open}}]}}
metrics:
  Open Share:
    expression: '{{[Open Orders]}} * 10 + {{[Orders Count]}}'
  Buying Customers:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: count
  Orders per Customer:
    type: reaggregate
    measure: Orders Count
    per: [Customer]
    aggregation: count
    population: dimension
  Running Orders:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
  Rolling Orders:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    window: 1
  Orders QTD:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    grainToDate: quarter
  Rolling Period Count:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    cumulativeType: count
    window: 1
  Running Orders Text:
    type: cumulative
    measure: Orders Count
    timeDimension: Order Month
    dataType: string
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
"""

DEC, JAN, FEB, MAR = date(2023, 12, 1), date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)
APR, MAY = date(2024, 4, 1), date(2024, 5, 1)


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, "count_null_regions", [("id", 0)], [("r1",), ("r2",), ("r3",)])
    _create(target, "count_null_customers", [("id", 0)], [("c1",), ("c2",), ("c3",)])
    _create(
        target,
        "count_null_orders",
        [
            ("id", 0),
            ("region_id", 0),
            ("customer_id", 0),
            ("order_date", "Nullable(Date)" if target.dialect == "clickhouse" else 3),
            ("status", 0),
            ("amount", 1),
        ],
        [
            ("o1", "r1", "c1", "2024-01-05", "open", 1),
            ("o2", "r2", "c2", "2024-03-09", "closed", 2),
            ("o3", "r1", "c1", None, "closed", 4),
            ("o4", None, "c2", "2024-03-01", "open", 8),
        ],
    )
    _create(
        target,
        "count_null_returns",
        [("id", 0), ("region_id", 0), ("refund", 1)],
        [("x1", "r3", 5)],
    )
    yaml_text = MODEL_YAML.format(schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _cell(value: Any) -> Any:
    """Dates as dates, numbers as two-place Decimals, text as it is."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date | str) or value is None:
        return value
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _rows(
    target: VendorTarget, model: SemanticModel, keys: list[str], measures: list[str], **query: Any
) -> list[tuple[Any, ...]]:
    """The rows, keys first, sorted with NULL keys last."""
    query_object = QueryObject(select={"dimensions": keys, "measures": measures}, **query)
    result = [
        tuple(_cell(row[name.lower()]) for name in [*keys, *measures])
        for row in _run(target, model, query_object)
    ]
    return sorted(result, key=lambda r: tuple((v is None, v) for v in r[: len(keys)]))


def _n(*values: float) -> tuple[Decimal, ...]:
    return tuple(Decimal(str(v)).quantize(Decimal("0.01")) for v in values)


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)

    # filterContext: r2 has no open order, the NULL region one; no order is
    # lost anywhere. The text count is '0' / '1', and a formula, a HAVING and
    # an ORDER BY read the 0 too.
    measures = ["Orders Count", "Open Orders", "Open Customers", "Lost Orders", "Open Share"]
    assert _rows(target, model, ["Region"], measures) == [
        ("r1", *_n(2, 1, 1, 0, 12)),
        ("r2", *_n(1, 0, 0, 0, 1)),
        (None, *_n(1, 1, 1, 0, 11)),
    ], target.name
    texts = _rows(target, model, ["Region"], ["Open Orders Text"])
    assert [(region, text.strip()) for region, text in texts] == [
        ("r1", "1"),
        ("r2", "0"),
        (None, "1"),
    ], target.name
    having = [{"field": "Open Orders", "op": "equals", "value": 0}]
    assert _rows(target, model, ["Region"], ["Open Orders"], having=having) == [("r2", *_n(0))], (
        target.name
    )
    ordered = _run(
        target,
        model,
        QueryObject(
            select={"dimensions": ["Region"], "measures": ["Open Orders"]},
            orderBy=[{"field": "Open Orders"}, {"field": "Region"}],
            limit=1,
        ),
    )
    assert [row["region"] for row in ordered] == ["r2"], target.name

    # Reaggregate: r3 has returns and no buyer.
    assert _rows(target, model, ["Region"], ["Buying Customers", "Refunds"]) == [
        ("r1", *_n(1), None),
        ("r2", *_n(1), None),
        ("r3", *_n(0, 5)),
        (None, *_n(1), None),
    ], target.name

    # asOf: February is outside a one-month window and r2's first month is
    # March; before any order every count is 0.
    counts = ["Running Orders", "Rolling Orders", "Orders QTD", "Rolling Period Count"]
    assert _rows(target, model, ["Region"], counts, asOf="2024-02-15") == [
        ("r1", *_n(1, 0, 1, 0)),
        ("r2", *_n(0, 0, 0, 0)),
        (None, *_n(0, 0, 0, 0)),
    ], target.name
    assert _rows(target, model, [], counts, asOf="2023-12-15") == [_n(0, 0, 0, 0)], target.name
    # A text count reads '0' too, as of a date, under a time filter and filled.
    text = _rows(target, model, ["Region"], ["Running Orders Text"], asOf="2024-02-15")
    assert [(region, value.strip()) for region, value in text] == [
        ("r1", "1"),
        ("r2", "0"),
        (None, "0"),
    ], target.name
    march = [{"field": "Order Month", "op": "between", "value": ["2024-03-01", "2024-03-31"]}]
    text = _rows(target, model, ["Region", "Order Month"], ["Running Orders Text"], where=march)
    assert [(region, value.strip()) for region, _, value in text] == [
        ("r2", "1"),
        (None, "1"),
    ], target.name

    # fill: before the first month, a rolling window run out, a new quarter.
    sums = ["Running Orders", "Rolling Orders", "Orders QTD"]
    wider = [{"field": "Order Month", "op": "between", "value": ["2023-12-01", "2024-05-31"]}]
    filled = _rows(target, model, ["Order Month"], sums, where=wider, fill=True)
    assert [row for row in filled if row[0] is not None] == [
        (DEC, *_n(0, 0, 0)),
        (JAN, *_n(1, 1, 1)),
        (FEB, *_n(1, 0, 1)),
        (MAR, *_n(3, 2, 3)),
        (APR, *_n(3, 0, 0)),
        (MAY, *_n(3, 0, 0)),
    ], target.name
    text = _rows(target, model, ["Order Month"], ["Running Orders Text"], where=wider, fill=True)
    assert [value.strip() for month, value in text if month is not None] == [
        "0",
        "1",
        "1",
        "3",
        "3",
        "3",
    ], target.name
    # From February, the window still reads January.
    later = [{"field": "Order Month", "op": "between", "value": ["2024-02-01", "2024-05-31"]}]
    filled = _rows(target, model, ["Order Month"], sums, where=later, fill=True)
    assert [row for row in filled if row[0] is not None] == [
        (FEB, *_n(1, 0, 1)),
        (MAR, *_n(3, 2, 3)),
        (APR, *_n(3, 0, 0)),
        (MAY, *_n(3, 0, 0)),
    ], target.name

    # population: c1's January carries into March; c2 has nothing before
    # March, c3 nothing at all.
    population = _rows(
        target,
        model,
        ["Customer", "Order Month"],
        ["Orders per Customer", "Running Orders", "Running Revenue"],
    )
    assert [row for row in population if row[1] is not None] == [
        ("c1", JAN, *_n(1, 1, 1)),
        ("c1", MAR, *_n(1, 1, 1)),
        ("c2", JAN, *_n(1, 0), None),
        ("c2", MAR, *_n(1, 2, 10)),
        ("c3", JAN, *_n(1, 0), None),
        ("c3", MAR, *_n(1, 0), None),
    ], target.name

    # Without the month, each member reads its running total as of March (as
    # of the date): c1's January carries on, c3 has none.
    measures = ["Orders per Customer", "Running Orders", "Rolling Period Count", "Running Revenue"]
    assert _rows(target, model, ["Customer"], measures, where=march) == [
        ("c1", *_n(1, 1, 0, 1)),
        ("c2", *_n(1, 2, 1, 10)),
        ("c3", *_n(1, 0, 0), None),
    ], target.name
    assert _rows(target, model, ["Customer"], measures, asOf="2024-02-15") == [
        ("c1", *_n(1, 1, 0, 1)),
        ("c2", *_n(1, 0, 0), None),
        ("c3", *_n(1, 0, 0), None),
    ], target.name


def test_duckdb_count_never_null(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_count_never_null(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_count_never_null(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_count_never_null(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_count_never_null(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_count_never_null(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_count_never_null(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_count_never_null(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
