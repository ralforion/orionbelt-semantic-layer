"""A federated median, percentile or correlation, from the fact leg's rows, on every engine.

The fact leg runs on the engine: its rows grouped by every value the measures
read, each group with the number of rows it stands for. The combine step
repeats each that many times and computes the measures in DuckDB. The rows
are the cases a group of equal values decides: one value many times under
one key, NULLs, an empty group, decimals near their width. The reference is
exact arithmetic (fractions), compared at a double's precision where DuckDB
answers in doubles.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pyarrow as pa
import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.federated_executor import combine, normalize_leg
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

# label -> (i, d, f, o) per row; o orders the rows for first and last.
VALUES: dict[str, list[tuple[int | None, str | None, float | None, int]]] = {
    "small": [
        (3, "1.50", 0.5, 1),
        (1, "2.25", 1.25, 2),
        (7, "-4.00", -2.0, 3),
        (3, "1.50", 0.5, 4),
        (3, "0.10", 0.125, 5),
        (5, "9.99", 3.5, 6),
    ],
    "repeated": [(2, "2.00", 2.0, o) for o in range(1, 8)] + [(9, "9.00", 9.0, 8)],
    "one": [(42, "42.42", 4.5, 1)],
    "nulls": [(None, None, None, 1), (10, "10.00", 1.0, 2), (20, "20.00", 2.0, 3)],
    "empty": [(None, None, None, 1), (None, None, None, 2)],
    "negative": [(-5, "-5.50", -5.5, 1), (5, "5.50", 5.5, 2), (-3, "-3.25", -3.25, 3)],
    "wide": [
        (10**15 + k, str(Decimal("9999999999999999.99") - k), float(k), k + 1) for k in range(5)
    ],
}

# (label, key, i, d, f, o): each label's rows under up to three keys, so equal
# rows meet under one key (rows 0 and 3 of a label) and across keys.
ROWS = [
    (label, 10 * n + r % 3, *row)
    for n, (label, rows) in enumerate(VALUES.items())
    for r, row in enumerate(rows)
]

MODEL = """\
version: 1.0
dataObjects:
  Facts:
    code: t
    dataSource: facts
    columns:
      Key: {code: k, abstractType: int}
      I: {code: i, abstractType: int}
      D: {code: d, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      F: {code: f, abstractType: float}
      O: {code: o, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Groups, columnsFrom: [Key], columnsTo: [Group Key]}
  Groups:
    code: g
    dataSource: groups
    columns:
      Group Key: {code: k, abstractType: int}
      Label: {code: l, abstractType: string}
dimensions:
  Label: {dataObject: Groups, column: Label, resultType: string}
measures:
  Median I: {columns: [{dataObject: Facts, column: I}], aggregation: median, resultType: float}
  P90 D:
    columns: [{dataObject: Facts, column: D}]
    aggregation: percentile_cont
    percentile: 0.9
    resultType: float
  P25 D:
    columns: [{dataObject: Facts, column: D}]
    aggregation: percentile_disc
    percentile: 0.25
    resultType: float
  First D:
    columns: [{dataObject: Facts, column: D}]
    aggregation: first
    withinGroup: {column: {dataObject: Facts, column: O}}
    resultType: float
  Last D:
    columns: [{dataObject: Facts, column: D}]
    aggregation: last
    withinGroup: {column: {dataObject: Facts, column: O}}
    resultType: float
  Corr I D:
    columns: [{dataObject: Facts, column: I}, {dataObject: Facts, column: D}]
    aggregation: corr
    resultType: float
  Var F: {columns: [{dataObject: Facts, column: F}], aggregation: variance, resultType: float}
  Positive Is:
    columns: [{dataObject: Facts, column: I}]
    aggregation: count_distinct
    resultType: int
    filters:
      - column: {dataObject: Facts, column: D}
        operator: gt
        values: [{dataType: int, valueInt: 0}]
  Distinct D:
    columns: [{dataObject: Facts, column: D}]
    aggregation: sum
    distinct: true
    resultType: float
  Average D Overall:
    columns: [{dataObject: Facts, column: D}]
    aggregation: avg
    total: true
    resultType: float
"""

MEASURES = [
    "Median I",
    "P90 D",
    "P25 D",
    "First D",
    "Last D",
    "Corr I D",
    "Var F",
    "Positive Is",
    "Distinct D",
    "Average D Overall",
    "Facts Count",
]

# int, decimal(18, 2), double, string
TYPES = {
    "postgres": ("BIGINT", "NUMERIC(18,2)", "DOUBLE PRECISION", "VARCHAR(20)"),
    "mysql": ("SIGNED", "DECIMAL(18,2)", "DOUBLE", "CHAR"),
    "snowflake": ("BIGINT", "NUMBER(18,2)", "DOUBLE", "VARCHAR"),
    "bigquery": ("INT64", "NUMERIC", "FLOAT64", "STRING"),
    "clickhouse": (
        "Nullable(Int64)",
        "Nullable(Decimal(18,2))",
        "Nullable(Float64)",
        "Nullable(String)",
    ),
    "dremio": ("BIGINT", "DECIMAL(18,2)", "DOUBLE", "VARCHAR"),
    "databricks": ("BIGINT", "DECIMAL(18,2)", "DOUBLE", "STRING"),
    "duckdb": ("BIGINT", "DECIMAL(18,2)", "DOUBLE", "VARCHAR"),
}


def _tables(dialect: str) -> str:
    """``WITH t AS (...), g AS (...)``: the rows, as tables of this engine."""
    whole, decimal, double, text = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST(NULL AS {kind})" if v is None else f"CAST('{v}' AS {kind})"

    facts = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(i, whole)} AS {q('i')}, "
        f"{value(d, decimal)} AS {q('d')}, {value(f, double)} AS {q('f')}, "
        f"{value(o, whole)} AS {q('o')}"
        for _, k, i, d, f, o in ROWS
    )
    groups = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(label, text)} AS {q('l')}"
        for k, label in sorted({(k, label) for label, k, *_ in ROWS})
    )
    return f"WITH {q('t')} AS ({facts}), {q('g')} AS ({groups})\n"


def _arrow(rows: list[dict[str, Any]]) -> pa.Table:
    return pa.table({name: [r[name] for r in rows] for name in rows[0]})


def _exact(v: object) -> Fraction:
    return Fraction(Decimal(str(v)))


def _median(values: list[Fraction]) -> Fraction | None:
    return _continuous(values, Fraction(1, 2))


def _continuous(values: list[Fraction], p: Fraction) -> Fraction | None:
    if not values:
        return None
    ordered = sorted(values)
    position = p * (len(ordered) - 1)
    low = math.floor(position)
    if low + 1 == len(ordered):
        return ordered[low]
    return ordered[low] + (ordered[low + 1] - ordered[low]) * (position - low)


def _discrete(values: list[Fraction], p: Fraction) -> Fraction | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(p * len(ordered)) - 1, 0)]


def _correlation(pairs: list[tuple[Fraction, Fraction]]) -> float | None:
    if len(pairs) < 2:
        return None
    n = len(pairs)
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx == 0 or syy == 0:
        return None
    return float(sxy) / math.sqrt(float(sxx) * float(syy))


def _expected(label: str) -> dict[str, Any]:
    rows = VALUES[label]
    i = [_exact(r[0]) for r in rows if r[0] is not None]
    d = [_exact(r[1]) for r in rows if r[1] is not None]
    f = [_exact(r[2]) for r in rows if r[2] is not None]
    ordered = sorted((r[3], _exact(r[1])) for r in rows if r[1] is not None)
    every_d = [_exact(r[1]) for rows in VALUES.values() for r in rows if r[1] is not None]
    spread = None
    if len(f) > 1:
        mean = sum(f) / len(f)
        spread = sum((v - mean) ** 2 for v in f) / (len(f) - 1)
    return {
        "Median I": _median(i),
        "P90 D": _continuous(d, Fraction(9, 10)),
        "P25 D": _discrete(d, Fraction(1, 4)),
        "First D": ordered[0][1] if ordered else None,
        "Last D": ordered[-1][1] if ordered else None,
        "Corr I D": _correlation(
            [(_exact(r[0]), _exact(r[1])) for r in rows if r[0] is not None and r[1] is not None]
        ),
        "Var F": spread,
        "Positive Is": len({r[0] for r in rows if r[0] is not None and r[1] and _exact(r[1]) > 0}),
        "Distinct D": sum(set(d)) if d else None,
        "Average D Overall": sum(every_d) / len(every_d),
        "Facts Count": len(rows),
    }


def _mismatches(target: VendorTarget) -> list[str]:
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": MEASURES}})
    plan = store.compile_query(store.load_model(MODEL).model_id, query, target.dialect).federation
    assert plan is not None
    assert plan.legs[0].rows is not None
    tables = {
        leg.name: normalize_leg(leg, _arrow(target.execute(_tables(target.dialect) + leg.sql)))
        for leg in plan.legs
    }
    rows = {r["Label"]: r for r in combine(plan, tables, None).arrow_table.to_pylist()}
    wrong = []
    for label in VALUES:
        for measure, want in _expected(label).items():
            got = rows[label][measure]
            if want is None or got is None:
                if want is not got:
                    wrong.append(f"{label} {measure}: {got} for {want}")
            elif isinstance(want, int):
                if got != want:
                    wrong.append(f"{label} {measure}: {got} for {want}")
            # The correlation of the 'wide' rows is -1 at 1e16, which doubles
            # do not hold apart: DuckDB's own CORR cancels there, one source too.
            elif measure == "Corr I D" and label == "wide":
                continue
            elif float(got) != pytest.approx(float(want), rel=1e-12):
                wrong.append(f"{label} {measure}: {got} for {float(want)}")
    return wrong


def _assert_exact(target: VendorTarget) -> None:
    assert _mismatches(target) == [], f"{target.name}: measures from rows differ"


def test_duckdb_federated_rows(vendor_duckdb: VendorTarget) -> None:
    _assert_exact(vendor_duckdb)


def test_postgres_federated_rows(vendor_postgres: VendorTarget) -> None:
    _assert_exact(vendor_postgres)


def test_mysql_federated_rows(vendor_mysql: VendorTarget) -> None:
    _assert_exact(vendor_mysql)


def test_clickhouse_federated_rows(vendor_clickhouse: VendorTarget) -> None:
    _assert_exact(vendor_clickhouse)


def test_snowflake_federated_rows(vendor_snowflake: VendorTarget) -> None:
    _assert_exact(vendor_snowflake)


def test_bigquery_federated_rows(vendor_bigquery: VendorTarget) -> None:
    _assert_exact(vendor_bigquery)


def test_databricks_federated_rows(vendor_databricks: VendorTarget) -> None:
    _assert_exact(vendor_databricks)


def test_dremio_federated_rows(vendor_dremio: VendorTarget) -> None:
    _assert_exact(vendor_dremio)
