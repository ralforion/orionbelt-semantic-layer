"""A federated ``variance`` or ``stddev`` is the exact one, whichever engine holds the facts.

The fact leg runs on the engine: per federation key, a count, a sum and a sum
of each value squared in the widest exact type the engine has
(``Dialect.exact_square``). The combine step computes the statistic from
their totals in 128-bit integers. The reference is exact arithmetic
(fractions), not the engine's own answer: Snowflake and Dremio cancel far
from zero on one source (``test_statistics_exec.py``), and a combined
statistic does not.
"""

from __future__ import annotations

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

# label -> (i, d2, d3) per row
VALUES: dict[str, list[tuple[int | None, str | None, str | None]]] = {
    "small": [
        (1, "1.10", "0.001"),
        (2, "2.25", "0.002"),
        (2, "2.25", "0.002"),
        (3, "3.40", "0.125"),
        (7, "7.05", "7.777"),
        (4, "4.00", "-0.004"),
    ],
    "far": [
        (10**9 + k, f"{10_000_000 + k / 100:.2f}", f"{1_000_000 + k / 1000:.3f}") for k in range(10)
    ],
    "big": [
        (10**15 + k, str(Decimal("9999999999999999.99") - Decimal(k) / 100), "1.000")
        for k in range(6)
    ],
    "huge": [(4 * 10**18 + k, "1.00", "1.000") for k in range(3)],
    "constant": [(123456789, "1234567.89", "123.456")] * 4,
    "one": [(42, "42.42", "4.242")],
    "nulls": [(None, None, None), (10, "10.00", "0.010"), (20, "20.00", "0.020")],
    "negative": [(-5, "-5.50", "-0.005"), (5, "5.50", "0.005"), (-3, "-3.25", "-3.250")],
}

# (label, key, i, d2, d3): each label's rows under up to three keys.
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
      D2: {code: d2, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      D3: {code: d3, abstractType: decimal, sqlPrecision: 18, sqlScale: 3}
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
  Var I: {columns: [{dataObject: Facts, column: I}], aggregation: variance, resultType: float}
  Var Pop I: {columns: [{dataObject: Facts, column: I}], aggregation: var_pop, resultType: float}
  SD I: {columns: [{dataObject: Facts, column: I}], aggregation: stddev, resultType: float}
  SD Pop D2:
    columns: [{dataObject: Facts, column: D2}]
    aggregation: stddev_pop
    resultType: float
  Var D2: {columns: [{dataObject: Facts, column: D2}], aggregation: variance, resultType: float}
  Var D3: {columns: [{dataObject: Facts, column: D3}], aggregation: variance, resultType: float}
"""

# measure -> (value column, sample, square root)
MEASURES = {
    "Var I": (0, True, False),
    "Var Pop I": (0, False, False),
    "SD I": (0, True, True),
    "SD Pop D2": (1, False, True),
    "Var D2": (1, True, False),
    "Var D3": (2, True, False),
}

# int, decimal(18, 2), decimal(18, 3), string
TYPES = {
    "postgres": ("BIGINT", "NUMERIC(18,2)", "NUMERIC(18,3)", "VARCHAR(20)"),
    "mysql": ("SIGNED", "DECIMAL(18,2)", "DECIMAL(18,3)", "CHAR"),
    "snowflake": ("BIGINT", "NUMBER(18,2)", "NUMBER(18,3)", "VARCHAR"),
    "bigquery": ("INT64", "NUMERIC", "NUMERIC", "STRING"),
    "clickhouse": (
        "Nullable(Int64)",
        "Nullable(Decimal(18,2))",
        "Nullable(Decimal(18,3))",
        "Nullable(String)",
    ),
    "dremio": ("BIGINT", "DECIMAL(18,2)", "DECIMAL(18,3)", "VARCHAR"),
    "databricks": ("BIGINT", "DECIMAL(18,2)", "DECIMAL(18,3)", "STRING"),
    "duckdb": ("BIGINT", "DECIMAL(18,2)", "DECIMAL(18,3)", "VARCHAR"),
}


def _tables(dialect: str) -> str:
    """``WITH t AS (...), g AS (...)``: the rows, as tables of this engine."""
    whole, two, three, text = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST(NULL AS {kind})" if v is None else f"CAST('{v}' AS {kind})"

    facts = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(i, whole)} AS {q('i')}, "
        f"{value(d2, two)} AS {q('d2')}, {value(d3, three)} AS {q('d3')}"
        for _, k, i, d2, d3 in ROWS
    )
    groups = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(label, text)} AS {q('l')}"
        for k, label in sorted({(k, label) for label, k, *_ in ROWS})
    )
    return f"WITH {q('t')} AS ({facts}), {q('g')} AS ({groups})\n"


def _arrow(rows: list[dict[str, Any]]) -> pa.Table:
    """*rows* as Arrow, a column of integers past 64 bits as decimals.

    Snowflake's driver hands a ``NUMBER(38, 0)`` sum of squares over as
    Python ints, which Arrow will not infer; through the executor it arrives
    as ``decimal128(38, 0)``, the type given here.
    """
    columns: dict[str, list[Any]] = {name: [r[name] for r in rows] for name in rows[0]}
    for name, values in columns.items():
        if any(isinstance(v, int) and not -(2**63) <= v < 2**63 for v in values):
            columns[name] = [None if v is None else Decimal(v) for v in values]
    return pa.table(columns)


def _variance(values: list[Any], *, sample: bool) -> Fraction | None:
    present = [Fraction(Decimal(str(v))) for v in values if v is not None]
    if len(present) < (2 if sample else 1):
        return None
    mean = sum(present) / len(present)
    spread = sum((v - mean) ** 2 for v in present)
    return spread / (len(present) - 1 if sample else len(present))


def _mismatches(target: VendorTarget) -> list[str]:
    store = ModelStore()
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Label"], "measures": list(MEASURES)}}
    )
    plan = store.compile_query(store.load_model(MODEL).model_id, query, target.dialect).federation
    assert plan is not None
    tables = {
        leg.name: normalize_leg(leg, _arrow(target.execute(_tables(target.dialect) + leg.sql)))
        for leg in plan.legs
    }
    rows = {r["Label"]: r for r in combine(plan, tables, None).arrow_table.to_pylist()}
    wrong = []
    for label, values in VALUES.items():
        for measure, (column, sample, root) in MEASURES.items():
            want = _variance([v[column] for v in values], sample=sample)
            got = rows[label][measure]
            if want is None or got is None:
                if want is not got:
                    wrong.append(f"{label} {measure}: {got} for {want}")
                continue
            value = Fraction(got) ** 2 if root else Fraction(got)
            if want == 0 and value == 0:
                continue
            if want == 0 or abs((value - want) / want) > Fraction(1, 10**14):
                wrong.append(f"{label} {measure}: {got} for {float(want)}")
    return wrong


def _assert_exact(target: VendorTarget) -> None:
    assert _mismatches(target) == [], f"{target.name}: combined statistics differ"


def test_duckdb_federated_statistics(vendor_duckdb: VendorTarget) -> None:
    _assert_exact(vendor_duckdb)


def test_postgres_federated_statistics(vendor_postgres: VendorTarget) -> None:
    _assert_exact(vendor_postgres)


def test_mysql_federated_statistics(vendor_mysql: VendorTarget) -> None:
    _assert_exact(vendor_mysql)


def test_clickhouse_federated_statistics(vendor_clickhouse: VendorTarget) -> None:
    _assert_exact(vendor_clickhouse)


def test_snowflake_federated_statistics(vendor_snowflake: VendorTarget) -> None:
    _assert_exact(vendor_snowflake)


def test_bigquery_federated_statistics(vendor_bigquery: VendorTarget) -> None:
    _assert_exact(vendor_bigquery)


def test_databricks_federated_statistics(vendor_databricks: VendorTarget) -> None:
    _assert_exact(vendor_databricks)


def test_dremio_federated_statistics(vendor_dremio: VendorTarget) -> None:
    _assert_exact(vendor_dremio)
