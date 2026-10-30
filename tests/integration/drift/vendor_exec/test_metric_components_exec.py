"""A derived metric reads its measures' variances and doubles as their own columns do.

A metric's formula inlines its components' aggregates without their casts,
so it skipped two rewrites a measure's own column gets with them: Dremio's
exact ``variance`` (``test_exact_variance_exec``) and a ``float`` measure's
sum taken over doubles, which an engine whose integer accumulator wraps needs.
Both keep every digit a double has. The exact ``AVG`` is left out: it stops at
the measure's own places, so the average of 1, 1 and 2 times 3 came back 3.99
on DuckDB, and the case below holds a metric to 4.00. Both sources are the
engine itself, as in ``test_statistics_exec``.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget
from .test_statistics_exec import TYPES

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
dataObjects:
  Values:
    code: t
    columns:
      Label: {code: l, abstractType: string}
      X: {code: x, abstractType: int}
      F: {code: f, abstractType: float}
dimensions:
  Label: {dataObject: Values, column: Label, resultType: string}
measures:
  X Avg: {columns: [{dataObject: Values, column: X}], aggregation: avg, resultType: int}
  X Total: {columns: [{dataObject: Values, column: X}], aggregation: sum, resultType: float}
  X Stddev: {columns: [{dataObject: Values, column: X}], aggregation: stddev, resultType: float}
  F Sum: {columns: [{dataObject: Values, column: F}], aggregation: sum, resultType: float}
metrics:
  Avg Thrice: {expression: "{[X Avg]} * 3", dataType: "decimal(18, 2)"}
  Avg Share: {expression: "{[X Avg]} / 600000", dataType: "decimal(18, 10)"}
  Total Again: {expression: "{[X Total]} * 1", dataType: double}
  Spread: {expression: "{[X Stddev]} * 1", dataType: double}
  Mixed: {expression: "{[X Avg]} + {[X Stddev]} + {[F Sum]}", dataType: double}
"""

#: The engines whose integer ``SUM`` of two rows of 9e18 answers; BigQuery and
#: Databricks raise, for the measure alone too.
SUMS_PAST_64_BITS = {"postgres", "mysql", "snowflake", "clickhouse", "dremio", "duckdb"}

#: (case, metric, rows as (x, f), expected, relative tolerance or None for
#: exact, engines or None for all).
CASES: list[tuple[str, str, list[tuple[Any, Any]], Any, float | None, set[str] | None]] = [
    # An average computed on in the formula: at the measure's own two places,
    # 1.33 * 3 is 3.99.
    ("thirds", "Avg Thrice", [(1, None), (1, None), (2, None)], Decimal("4.00"), None, None),
    # An average in a small quotient keeps its places.
    ("share", "Avg Share", [(1, None), (2, None)], Decimal("0.0000025000"), None, None),
    # A float measure's sum of integers past 64 bits, which wraps negative
    # where the accumulator does.
    (
        "wide",
        "Total Again",
        [(9000000000000000000, None), (9000000000000000000, None)],
        1.8e19,
        1e-12,
        SUMS_PAST_64_BITS,
    ),
    # Dremio's own standard deviation of these was 0. Snowflake's of an
    # integer keeps six places of the variance.
    ("far", "Spread", [(10**9 + i, None) for i in range(10)], math.sqrt(55 / 6), 1e-6, None),
    # An average, a deviation and a double sum in one formula.
    ("mixed", "Mixed", [(1, "0.5"), (2, "0.25"), (3, "0.25")], 4.0, 1e-9, None),
]


def _rows(dialect: str, label: str, rows: list[tuple[Any, Any]]) -> str:
    """``WITH t AS (...)``: *rows*, as a table of this engine."""
    text, whole, _, real = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST({'NULL' if v is None else repr(str(v))} AS {kind})"

    selects = " UNION ALL ".join(
        f"SELECT {value(label, text)} AS {q('l')}, {value(x, whole)} AS {q('x')}, "
        f"{value(f, real)} AS {q('f')}"
        for x, f in rows
    )
    return f"WITH {q('t')} AS ({selects})\n"


def _assert_exact_components(target: VendorTarget) -> None:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    wrong = []
    for case, metric, rows, expected, tolerance, engines in CASES:
        if engines is not None and target.dialect not in engines:
            continue
        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Label"], "measures": [metric]}}
        )
        sql = store.compile_query(model_id, query, target.dialect).sql
        try:
            (row,) = target.execute(_rows(target.dialect, case, rows) + sql)
        except Exception as exc:  # noqa: BLE001 - every engine's error is a wrong answer here
            wrong.append(f"{case}: {str(exc)[:200]}")
            continue
        got = {key.lower(): value for key, value in row.items()}[metric.lower()]
        if tolerance is None:
            ok = got is not None and Decimal(str(got)) == expected
        else:
            ok = got is not None and math.isclose(float(got), expected, rel_tol=tolerance)
        if not ok:
            wrong.append(f"{case}: {got!r}, expected {expected}")
    assert wrong == [], f"{target.name}: " + "; ".join(wrong)


def test_duckdb_metric_components(vendor_duckdb: VendorTarget) -> None:
    _assert_exact_components(vendor_duckdb)


def test_postgres_metric_components(vendor_postgres: VendorTarget) -> None:
    _assert_exact_components(vendor_postgres)


def test_mysql_metric_components(vendor_mysql: VendorTarget) -> None:
    _assert_exact_components(vendor_mysql)


def test_clickhouse_metric_components(vendor_clickhouse: VendorTarget) -> None:
    _assert_exact_components(vendor_clickhouse)


def test_snowflake_metric_components(vendor_snowflake: VendorTarget) -> None:
    _assert_exact_components(vendor_snowflake)


def test_bigquery_metric_components(vendor_bigquery: VendorTarget) -> None:
    _assert_exact_components(vendor_bigquery)


def test_databricks_metric_components(vendor_databricks: VendorTarget) -> None:
    _assert_exact_components(vendor_databricks)


def test_dremio_metric_components(vendor_dremio: VendorTarget) -> None:
    _assert_exact_components(vendor_dremio)
