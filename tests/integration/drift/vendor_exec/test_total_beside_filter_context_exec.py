"""A total computed apart beside a filter context's measure, on every engine.

A total no window combines (a median, a distinct count), and every total
under a rollup or cube, is computed again over the plan's rows. Beside a
measure with a filter context those rows were a CTE of finished values, so
the query was refused. The filter context's wrapper keeps the plan's tables
and filters in that CTE, and the total is now computed from them.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget
from .test_rollup_filter_context_exec import MODEL as BASE_MODEL
from .test_rollup_filter_context_exec import ROWS, _with_rows

pytestmark = pytest.mark.docker

MODEL = (
    BASE_MODEL
    + """\
  Median Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: median
    resultType: float
    total: true
  Cities Total:
    columns: [{dataObject: Sales, column: City}]
    aggregation: count_distinct
    total: true
"""
)

IN_N_AND_S = [{"field": "Region", "op": "in", "value": ["n", "s"]}]
KEPT = [r for r in ROWS if r[1] in {"n", "s"}]


def _run(target: VendorTarget, query: dict[str, Any]) -> list[dict[str, Any]]:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    sql = store.compile_query(model_id, QueryObject.model_validate(query), target.dialect).sql
    rows = target.execute(_with_rows(target.dialect, sql))
    return [{key.lower(): value for key, value in row.items()} for row in rows]


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _at_level(region: str | None, city: str | None, flags: tuple[int, int]) -> Decimal:
    """Revenue of any region at the row's level: every row of its region and
    city, the query's filter on the region dropped, a rolled-up one all."""
    return Decimal(
        sum(
            r[3]
            for r in ROWS
            if (flags[0] == 1 or r[1] == region) and (flags[1] == 1 or r[2] == city)
        )
    )


def _assert_totals(target: VendorTarget) -> None:
    wrong = []
    for grouping in ["rollup"] if target.dialect == "mysql" else ["rollup", "cube"]:
        query = {
            "select": {
                "dimensions": ["Region", "City"],
                "measures": ["Revenue Total", "Revenue Any Region"],
            },
            "where": IN_N_AND_S,
            "grouping": grouping,
        }
        for row in _run(target, query):
            flags = (int(row["_g_region"]), int(row["_g_city"]))
            got = (_number(row["revenue total"]), _number(row["revenue any region"]))
            want = (Decimal(sum(r[3] for r in KEPT)), _at_level(row["region"], row["city"], flags))
            if got != want:
                wrong.append(f"{grouping} {row['region']}, {row['city']}, {flags}: {got} != {want}")

    query = {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Median Total", "Cities Total", "Revenue Any Region"],
        },
        "where": IN_N_AND_S,
    }
    for row in _run(target, query):
        got = (
            _number(row["median total"]),
            _number(row["cities total"]),
            _number(row["revenue any region"]),
        )
        want = (
            Decimal(str(statistics.median(r[3] for r in KEPT))),
            Decimal(len({r[2] for r in KEPT})),
            Decimal(sum(r[3] for r in ROWS if r[1] == row["region"])),
        )
        if got != want:
            wrong.append(f"by region {row['region']}: {got} != {want}")
    assert not wrong, f"{target.name}:\n" + "\n".join(wrong)


def test_duckdb_total_beside_filter_context(vendor_duckdb: VendorTarget) -> None:
    _assert_totals(vendor_duckdb)


def test_postgres_total_beside_filter_context(vendor_postgres: VendorTarget) -> None:
    _assert_totals(vendor_postgres)


def test_mysql_total_beside_filter_context(vendor_mysql: VendorTarget) -> None:
    _assert_totals(vendor_mysql)


def test_clickhouse_total_beside_filter_context(vendor_clickhouse: VendorTarget) -> None:
    _assert_totals(vendor_clickhouse)


def test_snowflake_total_beside_filter_context(vendor_snowflake: VendorTarget) -> None:
    _assert_totals(vendor_snowflake)


def test_bigquery_total_beside_filter_context(vendor_bigquery: VendorTarget) -> None:
    _assert_totals(vendor_bigquery)


def test_databricks_total_beside_filter_context(vendor_databricks: VendorTarget) -> None:
    _assert_totals(vendor_databricks)


def test_dremio_total_beside_filter_context(vendor_dremio: VendorTarget) -> None:
    _assert_totals(vendor_dremio)
