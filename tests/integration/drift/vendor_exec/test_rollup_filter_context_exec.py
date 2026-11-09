"""A filter context's measure under a rollup or cube has its subtotals, on every engine.

The measure is a scan of its own, joined to the query's rows by their
dimensions. Under a rollup a subtotal's dimension is NULL, which met the
scan's group of a NULL member, or none: the subtotals and the grand total
read NULL, and the GROUPING() flags that tell a subtotal from a NULL member
were gone from the result, beside a total too. The scan now rolls up the same
dimensions and is joined on their flags as well, and the flags are returned.
"""

from __future__ import annotations

from decimal import Decimal
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
  Revenue: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum}
  Revenue Any Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    filterContext: {exclude: [Region]}
  Revenue All:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    filterContext: {mode: FIXED}
  Revenue Total: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum, total: true}
"""

# id, region, city, amount: NULL is a region of its own, which the query's
# filter leaves out and the filter contexts read.
ROWS: list[tuple[int, str | None, str, int]] = [
    (1, "n", "a", 10),
    (2, "n", "b", 20),
    (3, "s", "c", 40),
    (4, "n", "b", 60),
    (5, None, "d", 5),
    (6, None, "d", 7),
]
KEPT = {"n", "s"}

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


# The filter contexts' measures, and a total, which a query beside them
# refuses: each is computed again over the query's rows.
MEASURES = [["Revenue", "Revenue Any Region", "Revenue All"], ["Revenue", "Revenue Total"]]


def _run(target: VendorTarget, grouping: str, measures: list[str]) -> list[dict[str, Any]]:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    query = QueryObject.model_validate(
        {
            "select": {"dimensions": ["Region", "City"], "measures": measures},
            "where": [{"field": "Region", "op": "in", "value": sorted(KEPT)}],
            "grouping": grouping,
        }
    )
    sql = store.compile_query(model_id, query, target.dialect).sql
    rows = target.execute(_with_rows(target.dialect, sql))
    return [{key.lower(): value for key, value in row.items()} for row in rows]


def _sum(rows: list[tuple[int, str | None, str, int]]) -> Decimal | None:
    return Decimal(sum(r[3] for r in rows)) if rows else None


def _expected(region: str | None, city: str | None, flags: tuple[int, int]) -> dict[str, Any]:
    """The row's values, each at its grouping set's level: a rolled-up
    dimension takes every value, a kept one its own."""

    def matches(r: tuple[int, str | None, str, int]) -> bool:
        return (flags[0] == 1 or r[1] == region) and (flags[1] == 1 or r[2] == city)

    kept = [r for r in ROWS if r[1] in KEPT]
    return {
        "revenue": _sum([r for r in kept if matches(r)]),
        "revenue any region": _sum([r for r in ROWS if matches(r)]),
        "revenue all": _sum([r for r in ROWS if matches(r)]),
        "revenue total": _sum(kept),
    }


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _assert_subtotals(target: VendorTarget) -> None:
    groupings = ["rollup"] if target.dialect == "mysql" else ["rollup", "cube"]
    wrong = []
    for grouping in groupings:
        for measures in MEASURES:
            rows = _run(target, grouping, measures)
            assert rows, (target.name, grouping)
            for row in rows:
                flags = (int(row["_g_region"]), int(row["_g_city"]))
                expected = _expected(row["region"], row["city"], flags)
                want = {name.lower(): expected[name.lower()] for name in measures}
                got = {name: _number(row[name]) for name in want}
                if got != want:
                    wrong.append(
                        f"{grouping} {row['region']}, {row['city']}, {flags}: {got} != {want}"
                    )
    assert not wrong, f"{target.name}:\n" + "\n".join(wrong)


def test_duckdb_rollup_filter_context(vendor_duckdb: VendorTarget) -> None:
    _assert_subtotals(vendor_duckdb)


def test_postgres_rollup_filter_context(vendor_postgres: VendorTarget) -> None:
    _assert_subtotals(vendor_postgres)


def test_mysql_rollup_filter_context(vendor_mysql: VendorTarget) -> None:
    _assert_subtotals(vendor_mysql)


def test_clickhouse_rollup_filter_context(vendor_clickhouse: VendorTarget) -> None:
    _assert_subtotals(vendor_clickhouse)


def test_snowflake_rollup_filter_context(vendor_snowflake: VendorTarget) -> None:
    _assert_subtotals(vendor_snowflake)


def test_bigquery_rollup_filter_context(vendor_bigquery: VendorTarget) -> None:
    _assert_subtotals(vendor_bigquery)


def test_databricks_rollup_filter_context(vendor_databricks: VendorTarget) -> None:
    _assert_subtotals(vendor_databricks)


def test_dremio_rollup_filter_context(vendor_dremio: VendorTarget) -> None:
    _assert_subtotals(vendor_dremio)
