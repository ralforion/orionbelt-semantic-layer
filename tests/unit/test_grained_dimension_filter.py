"""A ``where`` filter on a grained dimension compares the period it shows.

======  ==========  ======  ======
order   date        amount  region
======  ==========  ======  ======
``o1``  2025-01-05  10      North
``o2``  2025-01-20  1       North
``o3``  2025-02-03  2       South
======  ==========  ======  ======

``Order Month = 2025-01-01`` is January, both orders; ``Order Month >=
2025-01-15`` starts in February, since January starts before the 15th. The
date column itself, through a dimension without a grain, still compares dates.
A ``filterContext.include`` item and the body of an ``exists`` filter compare
the period as ``where`` does.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: grained_filter
dataObjects:
  Regions:
    code: regions
    columns:
      Region Id: {code: id, abstractType: string, primaryKey: true}
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Order Date: {code: d, abstractType: date}
      Order Time: {code: t, abstractType: timestamp}
      Amount: {code: amount, abstractType: float}
      Order Region: {code: region, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Order Region]
        columnsTo: [Region Id]
dimensions:
  Region: {dataObject: Regions, column: Region Id, resultType: string}
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
  Order Week: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: week}
  Order Day: {dataObject: Orders, column: Order Date, resultType: date}
  Order Time Month:
    dataObject: Orders
    column: Order Time
    resultType: timestamp
    timeGrain: month
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  January Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    filterContext:
      mode: RELATIVE
      include:
        - {field: Order Month, op: equals, value: "2025-01-01"}
"""

JAN, FEB = date(2025, 1, 1), date(2025, 2, 1)


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
        "CREATE TABLE orders AS SELECT id, d, d + INTERVAL 10 HOUR AS t, amount, region"
        " FROM (VALUES"
        " ('o1', DATE '2025-01-05', 10.0, 'North'),"
        " ('o2', DATE '2025-01-20', 1.0, 'North'),"
        " ('o3', DATE '2025-02-03', 2.0, 'South')) v(id, d, amount, region)"
    )
    con.execute("CREATE TABLE regions AS SELECT * FROM (VALUES ('North'), ('South')) v(id)")
    return con


def _revenue(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, dimension: str, where: list
) -> dict:
    query = QueryObject(select={"dimensions": [dimension], "measures": ["Revenue"]}, where=where)
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    return {_day(key): value for key, value in con.execute(sql).fetchall()}


def _day(value: object) -> object:
    return value.date() if hasattr(value, "date") and callable(value.date) else value


@pytest.mark.parametrize(
    ("field", "op", "value", "expected"),
    [
        ("Order Month", "equals", "2025-01-01", {JAN: Decimal("11.00")}),
        ("Order Month", "equals", "2025-01-20", {}),
        ("Order Month", "gte", "2025-01-15", {FEB: Decimal("2.00")}),
        ("Order Month", "lte", "2025-01-15", {JAN: Decimal("11.00")}),
        ("Order Month", "between", ["2025-01-02", "2025-02-28"], {FEB: Decimal("2.00")}),
        (
            "Order Time Month",
            "gte",
            "2025-01-01 00:00:00",
            {JAN: Decimal("11.00"), FEB: Decimal("2.00")},
        ),
        ("Order Time Month", "gt", "2025-01-01 00:00:00", {FEB: Decimal("2.00")}),
    ],
    ids=["equals-start", "equals-inside", "gte-inside", "lte-inside", "between", "ts-gte", "ts-gt"],
)
def test_the_filter_compares_the_month(
    con: duckdb.DuckDBPyConnection,
    model: SemanticModel,
    field: str,
    op: str,
    value: object,
    expected: dict,
) -> None:
    dimension = "Order Time Month" if field == "Order Time Month" else "Order Month"
    assert _revenue(con, model, dimension, [{"field": field, "op": op, "value": value}]) == expected


def test_a_week_compares_its_monday(con: duckdb.DuckDBPyConnection, model: SemanticModel) -> None:
    """2025-01-20 is a Monday: its week holds ``o2`` only."""
    where = [{"field": "Order Week", "op": "equals", "value": "2025-01-20"}]
    assert _revenue(con, model, "Order Week", where) == {date(2025, 1, 20): Decimal("1.00")}


def test_the_date_itself_still_compares_dates(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """``Order Day`` has no grain: ``>= Jan 15`` keeps ``o2`` in January."""
    where = [{"field": "Order Day", "op": "gte", "value": "2025-01-15"}]
    assert _revenue(con, model, "Order Month", where) == {
        JAN: Decimal("1.00"),
        FEB: Decimal("2.00"),
    }


def test_the_filter_means_the_same_at_another_grain_shown(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """Shown by day, ``Order Month >= Jan 15`` still compares months."""
    where = [{"field": "Order Month", "op": "gte", "value": "2025-01-15"}]
    assert _revenue(con, model, "Order Month:day", where) == {date(2025, 2, 3): Decimal("2.00")}


def test_an_include_filter_compares_the_month(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """``filterContext.include`` of ``Order Month = 2025-01-01`` is January, as in ``where``."""
    query = QueryObject(select={"measures": ["January Revenue"]})
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    assert con.execute(sql).fetchall() == [(Decimal("11.00"),)]


def test_an_exists_filter_compares_the_month(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    """North has orders in January, none on 1 January: the month finds it."""
    query = QueryObject(
        select={"dimensions": ["Region"]},
        where=[
            {
                "field": "Region",
                "op": "exists",
                "subquery": {
                    "dataObject": "Orders",
                    "filter": [{"field": "Order Month", "op": "equals", "value": "2025-01-01"}],
                },
            }
        ],
    )
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    assert con.execute(sql).fetchall() == [("North",)]


def test_the_period_is_cast_as_the_dimension_is_shown(model: SemanticModel) -> None:
    """ClickHouse shows a ``timestamp`` dimension as ``DateTime64(3)``. The
    filter's period has to be the same type: cast to ``DateTime``, which ends in
    2106, a 2110 month would never compare equal to its own row."""
    query = QueryObject(
        select={"dimensions": ["Order Time Month"], "measures": ["Revenue"]},
        where=[{"field": "Order Time Month", "op": "gte", "value": "2110-01-01 00:00:00"}],
    )
    sql = CompilationPipeline().compile(query, model, "clickhouse").sql
    where = sql[sql.index("WHERE") :]
    assert "DateTime64(3)" in where
    assert "AS Nullable(DateTime))" not in where and "AS DateTime)" not in where
