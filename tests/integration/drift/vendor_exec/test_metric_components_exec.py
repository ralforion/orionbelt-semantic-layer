"""A derived metric reads its measures' averages and variances as their own columns do.

A metric's formula inlines its components' aggregates without their casts, so
it skipped the rewrites a measure's own column gets with them: the exact
``AVG`` (``test_exact_avg_exec``) and Dremio's exact ``variance``
(``test_exact_variance_exec``). Each case runs a metric over the values that
told the rewrite apart, and one formula mixes decimal and double components,
which some engines type apart. An integer ``SUM`` is read as the engine sums
it: widened, Dremio divides it at 6 places (``test_decimal_division_exec``).
Both sources are the engine itself, as in ``test_statistics_exec``.
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
      D: {code: d, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      F: {code: f, abstractType: float}
dimensions:
  Label: {dataObject: Values, column: Label, resultType: string}
measures:
  X Avg: {columns: [{dataObject: Values, column: X}], aggregation: avg, resultType: int}
  X Stddev: {columns: [{dataObject: Values, column: X}], aggregation: stddev, resultType: float}
  D Avg: {columns: [{dataObject: Values, column: D}], aggregation: avg, resultType: decimal}
  F Sum: {columns: [{dataObject: Values, column: F}], aggregation: sum, resultType: float}
metrics:
  Avg Offset: {expression: "{[X Avg]} - 1000000000000000000", dataType: "decimal(38, 2)"}
  Avg Share: {expression: "{[X Avg]} / 600000", dataType: "decimal(18, 10)"}
  Spread: {expression: "{[X Stddev]} * 1", dataType: double}
  Mixed: {expression: "{[X Avg]} + {[X Stddev]} + {[F Sum]}", dataType: double}
  Decimal Mean: {expression: "{[D Avg]} * 1", dataType: "decimal(38, 2)"}
"""

#: (case, metric, rows as (x, d, f), expected, relative tolerance or None for exact).
CASES: list[tuple[str, str, list[tuple[Any, Any, Any]], Any, float | None]] = [
    # A double average of these is 1e18, and the offset 0.
    (
        "avg",
        "Avg Offset",
        [(1000000000000000002, None, None), (1000000000000000004, None, None)],
        Decimal("3"),
        None,
    ),
    # An exact average in a small quotient keeps its places, which a 38-digit
    # decimal dividend would not on Dremio.
    ("share", "Avg Share", [(1, None, None), (2, None, None)], Decimal("0.0000025000"), None),
    # Dremio's own standard deviation of these was 0. Snowflake's of an
    # integer keeps six places of the variance.
    ("far", "Spread", [(10**9 + i, None, None) for i in range(10)], math.sqrt(55 / 6), 1e-6),
    # An exact average, a double deviation and a double sum in one formula.
    ("mixed", "Mixed", [(1, None, "0.5"), (2, None, "0.25"), (3, None, "0.25")], 4.0, 1e-9),
    # A double average of these is .56; the exact one rounds to .57.
    (
        "tie",
        "Decimal Mean",
        [(None, "12345678901234.56", None), (None, "12345678901234.57", None)],
        Decimal("12345678901234.57"),
        None,
    ),
]


def _rows(dialect: str, label: str, rows: list[tuple[Any, Any, Any]]) -> str:
    """``WITH t AS (...)``: *rows*, as a table of this engine."""
    text, whole, fixed, real = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST({'NULL' if v is None else repr(str(v))} AS {kind})"

    selects = " UNION ALL ".join(
        f"SELECT {value(label, text)} AS {q('l')}, {value(x, whole)} AS {q('x')}, "
        f"{value(d, fixed)} AS {q('d')}, {value(f, real)} AS {q('f')}"
        for x, d, f in rows
    )
    return f"WITH {q('t')} AS ({selects})\n"


def _assert_exact_components(target: VendorTarget) -> None:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    wrong = []
    for case, metric, rows, expected, tolerance in CASES:
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
