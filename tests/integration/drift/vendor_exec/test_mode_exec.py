"""``mode`` answers the smallest of the most frequent values, on every engine that can.

Two values as frequent as each other are a tie, and engines settle it as they
please: DuckDB's ``MODE`` answered one or the other from one run to the next
(15 runs, 15 answers over 200 tied groups), ClickHouse's approximate ``topK``
too, and 0 for a group of NULLs only. PostgreSQL answers the smallest, and so
do Databricks, DuckDB and ClickHouse as OBSL renders them. Snowflake's
``MODE`` and BigQuery's approximate ``APPROX_TOP_COUNT`` answer one of the most
frequent values, and are held to that. Ties are inserted in either order, for
integers, strings, decimals and dates.
"""

from __future__ import annotations

from datetime import date
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
  V:
    code: v
    columns:
      G: {code: g, abstractType: string}
      N: {code: n, abstractType: int}
      S: {code: s, abstractType: string}
      D: {code: d, abstractType: decimal, sqlPrecision: 10, sqlScale: 2}
      T: {code: t, abstractType: date}
dimensions:
  G: {dataObject: V, column: G, resultType: string}
measures:
  Mode N: {columns: [{dataObject: V, column: N}], aggregation: mode, resultType: int}
  Mode S: {columns: [{dataObject: V, column: S}], aggregation: mode, resultType: string}
  Mode D: {columns: [{dataObject: V, column: D}], aggregation: mode, resultType: decimal}
  Mode T: {columns: [{dataObject: V, column: T}], aggregation: mode, resultType: date}
"""

#: The values of each group, by column; each group's rows are its values in order.
GROUPS: dict[str, dict[str, list[Any]]] = {
    "tie_up": {
        "N": [3, 5, 3, 5],
        "S": ["a", "b", "a", "b"],
        "D": ["3.25", "5.50", "3.25", "5.50"],
        "T": ["2024-01-03", "2024-01-05", "2024-01-03", "2024-01-05"],
    },
    "tie_down": {
        "N": [5, 3, 5, 3],
        "S": ["b", "a", "b", "a"],
        "D": ["5.50", "3.25", "5.50", "3.25"],
        "T": ["2024-01-05", "2024-01-03", "2024-01-05", "2024-01-03"],
    },
    "three_way": {
        "N": [9, 1, 9, 2, 1, 2],
        "S": ["z", "c", "z", "d", "c", "d"],
        "D": ["9.00", "1.00", "9.00", "2.00", "1.00", "2.00"],
        "T": ["2024-09-01", "2024-01-01", "2024-09-01", "2024-02-01", "2024-01-01", "2024-02-01"],
    },
    "clear": {
        "N": [4, 7, 7, 4, 7],
        "S": ["x", "y", "y", "x", "y"],
        "D": ["4.00", "7.00", "7.00", "4.00", "7.00"],
        "T": ["2024-04-01", "2024-07-01", "2024-07-01", "2024-04-01", "2024-07-01"],
    },
    "nulls": {
        "N": [None, None, None, 6],
        "S": [None, None, None, "q"],
        "D": [None, None, None, "6.00"],
        "T": [None, None, None, "2024-06-01"],
    },
    "only_nulls": {c: [None, None] for c in ("N", "S", "D", "T")},
}

#: string, integer, decimal(10, 2), date, as each engine spells them in a CAST.
TYPES = {
    "postgres": ("VARCHAR(20)", "BIGINT", "NUMERIC(10,2)", "DATE"),
    "snowflake": ("VARCHAR", "BIGINT", "NUMBER(10,2)", "DATE"),
    "bigquery": ("STRING", "INT64", "NUMERIC", "DATE"),
    "clickhouse": (
        "Nullable(String)",
        "Nullable(Int64)",
        "Nullable(Decimal(10, 2))",
        "Nullable(Date)",
    ),
    "databricks": ("STRING", "BIGINT", "DECIMAL(10,2)", "DATE"),
    "duckdb": ("VARCHAR", "BIGINT", "DECIMAL(10,2)", "DATE"),
}

#: The engines whose ``mode`` OBSL settles on the smallest of the most frequent.
SMALLEST = {"postgres", "databricks", "duckdb", "clickhouse"}


def _typed(column: str, value: Any) -> Any:
    if value is None:
        return None
    return {"N": int, "S": str, "D": Decimal, "T": date.fromisoformat}[column](value)


def _most_frequent(values: list[Any]) -> set[Any]:
    present = [v for v in values if v is not None]
    if not present:
        return {None}
    top = max(present.count(v) for v in present)
    return {v for v in present if present.count(v) == top}


def _rows(dialect: str) -> str:
    text, whole, fixed, day = TYPES[dialect]
    kinds = {"N": whole, "S": text, "D": fixed, "T": day}
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST({'NULL' if v is None else repr(str(v))} AS {kind})"

    rows = " UNION ALL ".join(
        f"SELECT {value(label, text)} AS {q('g')}, "
        + ", ".join(f"{value(group[c][i], kinds[c])} AS {q(c.lower())}" for c in kinds)
        for label, group in GROUPS.items()
        for i in range(len(group["N"]))
    )
    return f"WITH {q('v')} AS ({rows})\n"


def _wrong(target: VendorTarget) -> list[str]:
    measures = [f"Mode {c}" for c in ("N", "S", "D", "T")]
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"dimensions": ["G"], "measures": measures}})
    sql = store.compile_query(store.load_model(MODEL).model_id, query, target.dialect).sql
    wrong = []
    # Three runs: a tie settled by the order rows are read in moves between them.
    for _ in range(3):
        for row in target.execute(_rows(target.dialect) + sql):
            group = GROUPS[row["G"]]
            for measure in measures:
                column = measure[-1]
                got = row[measure]
                if isinstance(got, str) and column in ("D", "T"):
                    got = _typed(column, got)
                frequent = {_typed(column, v) for v in _most_frequent(group[column])}
                if target.dialect in SMALLEST:
                    present = sorted(v for v in frequent if v is not None)
                    want: set[Any] = {present[0] if present else None}
                else:
                    want = frequent
                if got not in want:
                    wrong.append(f"{row['G']} {measure}: {got!r}, want one of {want}")
    return wrong


def test_duckdb_mode(vendor_duckdb: VendorTarget) -> None:
    assert _wrong(vendor_duckdb) == []


def test_postgres_mode(vendor_postgres: VendorTarget) -> None:
    assert _wrong(vendor_postgres) == []


def test_clickhouse_mode(vendor_clickhouse: VendorTarget) -> None:
    assert _wrong(vendor_clickhouse) == []


def test_snowflake_mode(vendor_snowflake: VendorTarget) -> None:
    assert _wrong(vendor_snowflake) == []


def test_bigquery_mode(vendor_bigquery: VendorTarget) -> None:
    assert _wrong(vendor_bigquery) == []


def test_databricks_mode(vendor_databricks: VendorTarget) -> None:
    assert _wrong(vendor_databricks) == []
