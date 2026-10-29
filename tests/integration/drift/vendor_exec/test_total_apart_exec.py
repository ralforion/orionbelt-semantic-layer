"""Totals and grain overrides computed apart, on each of the eight engines.

A total or grain override no window over the grouped rows combines - a
standard deviation, a distinct count, a median - is computed again over the
plan's rows in a CTE of its own and joined back: cross joined for a total, on
the grain's dimensions for a grain override, a NULL member to its own group.
Under ``rollup`` every total is, since the grouped rows include the subtotals.
The reference is the exact value over the rows, computed here.
"""

from __future__ import annotations

import statistics
from fractions import Fraction
from typing import Any

import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
dataObjects:
  Sales:
    code: t
    columns:
      ID: {code: id, abstractType: int}
      Region: {code: region, abstractType: string}
      City: {code: city, abstractType: string}
      Amount: {code: amount, abstractType: int}
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  City: {dataObject: Sales, column: City, resultType: string}
measures:
  Revenue Total: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum, total: true}
  SD Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: stddev
    resultType: float
    total: true
  Amounts Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: count_distinct
    total: true
  Median Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: median
    resultType: float
    total: true
  SD Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: stddev
    resultType: float
    grain: {mode: FIXED, keepOnly: [Region]}
  Amounts Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: count_distinct
    grain: {mode: FIXED, keepOnly: [Region]}
"""

# id, region, city, amount: region n spans two cities, NULL is a region of its
# own, and the amount 10 recurs in another group.
ROWS: list[tuple[int, str | None, str, int]] = [
    (1, "n", "a", 10),
    (2, "n", "b", 20),
    (3, "s", "c", 40),
    (4, "n", "b", 60),
    (5, None, "d", 5),
    (6, None, "d", 7),
    (7, "s", "c", 40),
    (8, "s", "c", 10),
]

# int, string
TYPES = {
    "postgres": ("BIGINT", "VARCHAR(10)"),
    "mysql": ("SIGNED", "CHAR"),
    "snowflake": ("BIGINT", "VARCHAR"),
    "bigquery": ("INT64", "STRING"),
    "clickhouse": ("Nullable(Int64)", "Nullable(String)"),
    "dremio": ("BIGINT", "VARCHAR"),
    "databricks": ("BIGINT", "STRING"),
    "duckdb": ("BIGINT", "VARCHAR"),
}


def _with_rows(dialect: str, sql: str) -> str:
    """*sql*, a ``WITH`` query, with the rows as a CTE ``t`` ahead of its own."""
    whole, text = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST(NULL AS {kind})" if v is None else f"CAST('{v}' AS {kind})"

    rows = " UNION ALL ".join(
        f"SELECT {value(i, whole)} AS {q('id')}, {value(region, text)} AS {q('region')}, "
        f"{value(city, text)} AS {q('city')}, {value(amount, whole)} AS {q('amount')}"
        for i, region, city, amount in ROWS
    )
    assert sql.startswith("WITH ")
    return f"WITH {q('t')} AS ({rows}), {sql[len('WITH ') :]}"


def _run(target: VendorTarget, query: dict[str, Any]) -> list[dict[str, Any]]:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    sql = store.compile_query(model_id, QueryObject.model_validate(query), target.dialect).sql
    rows = target.execute(_with_rows(target.dialect, sql))
    return [{key.lower(): value for key, value in row.items()} for row in rows]


def _stddev(values: list[int]) -> float | None:
    return statistics.stdev(values) if len(values) > 1 else None


def _close(got: Any, want: float | None) -> bool:
    if want is None or got is None:
        return got is want
    return abs(Fraction(str(got)) - Fraction(want)) <= Fraction(1, 10**9) * max(1, abs(want))


def _mismatches(target: VendorTarget) -> list[str]:
    amounts = [a for *_, a in ROWS]
    wrong: list[str] = []
    rows = _run(
        target,
        {
            "select": {
                "dimensions": ["Region", "City"],
                "measures": ["Revenue Total", "SD Total", "Amounts Total", "Median Total"],
            }
        },
    )
    if len(rows) != 4:
        wrong.append(f"totals: {len(rows)} rows for 4")
    for row in rows:
        if int(row["revenue total"]) != sum(amounts):
            wrong.append(f"revenue total {row['revenue total']}")
        if not _close(row["sd total"], _stddev(amounts)):
            wrong.append(f"sd total {row['sd total']}")
        if int(row["amounts total"]) != len(set(amounts)):
            wrong.append(f"amounts total {row['amounts total']}")
        if not _close(row["median total"], statistics.median(amounts)):
            wrong.append(f"median total {row['median total']}")

    rows = _run(
        target,
        {"select": {"dimensions": ["Region", "City"], "measures": ["SD Region", "Amounts Region"]}},
    )
    for row in rows:
        mine = [r for r in ROWS if r[1] == row["region"]]
        if not _close(row["sd region"], _stddev([r[3] for r in mine])):
            wrong.append(f"sd region {row['region']}: {row['sd region']}")
        if int(row["amounts region"]) != len({r[3] for r in mine}):
            wrong.append(f"amounts region {row['region']}: {row['amounts region']}")

    rows = _run(
        target,
        {
            "select": {"dimensions": ["Region", "City"], "measures": ["Revenue Total", "SD Total"]},
            "grouping": "rollup",
        },
    )
    if len(rows) != 8:
        wrong.append(f"rollup: {len(rows)} rows for 8")
    for row in rows:
        if int(row["revenue total"]) != sum(amounts):
            wrong.append(f"rollup revenue total {row['revenue total']}")
        if not _close(row["sd total"], _stddev(amounts)):
            wrong.append(f"rollup sd total {row['sd total']}")
    return wrong


def _assert_apart(target: VendorTarget) -> None:
    assert _mismatches(target) == [], target.name


def test_duckdb_total_apart(vendor_duckdb: VendorTarget) -> None:
    _assert_apart(vendor_duckdb)


def test_postgres_total_apart(vendor_postgres: VendorTarget) -> None:
    _assert_apart(vendor_postgres)


def test_mysql_total_apart(vendor_mysql: VendorTarget) -> None:
    _assert_apart(vendor_mysql)


def test_clickhouse_total_apart(vendor_clickhouse: VendorTarget) -> None:
    _assert_apart(vendor_clickhouse)


def test_snowflake_total_apart(vendor_snowflake: VendorTarget) -> None:
    _assert_apart(vendor_snowflake)


def test_bigquery_total_apart(vendor_bigquery: VendorTarget) -> None:
    _assert_apart(vendor_bigquery)


def test_databricks_total_apart(vendor_databricks: VendorTarget) -> None:
    _assert_apart(vendor_databricks)


def test_dremio_total_apart(vendor_dremio: VendorTarget) -> None:
    _assert_apart(vendor_dremio)
