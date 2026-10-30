"""The statistical aggregates, run on each of the eight engines.

``stddev`` and ``variance`` are the sample statistics. MySQL's ``STDDEV`` and
``VARIANCE`` are its population ones, and ClickHouse's ``varSamp`` family
subtracts sums of squares, which cancel on values far from zero: 10^9 + 0..9
had a variance of 0 there. Each statistic is asserted against the exact one
of its rows (fractions), within what an engine's double keeps, over integers,
decimals and doubles: a group of one value, a constant group, a group with a
NULL and a group far from zero. Where the SQL standard leaves a statistic
undefined - a sample statistic of one value, a correlation of a constant - the
answer is NULL, not ``NaN`` or ``inf``.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

SINGLE = ("stddev", "stddev_pop", "variance", "var_pop")
PAIRED = ("covar_samp", "covar_pop", "corr", "regr_slope", "regr_intercept")

#: (label, integer, second integer, decimal, double), written as the engine reads them.
ROWS: list[tuple[str, int | None, int | None, str | None, str | None]] = [
    ("small", 1, 2, "1.10", "0.5"),
    ("small", 2, 1, "2.25", "1.5"),
    ("small", 2, 4, "2.25", "2.5"),
    ("small", 3, 3, "3.40", "3.5"),
    ("small", 7, 9, "7.05", "7.5"),
    ("small", 4, 5, "4.00", "4.5"),
    ("one", 42, 7, "42.42", "4.25"),
    ("flat", 5, 1, "5.50", "0.25"),
    ("flat", 5, 2, "5.50", "0.25"),
    ("flat", 5, 3, "5.50", "0.25"),
    ("level", 1, 4, "1.00", "1.0"),
    ("level", 2, 4, "2.00", "2.0"),
    ("level", 6, 4, "6.00", "6.0"),
    ("nulls", None, None, None, None),
    ("nulls", 10, 3, "10.00", "1.0"),
    ("nulls", 20, 8, "20.00", "2.0"),
    *(
        ("far", 10**9 + i, 10**9 + 2 * i + i % 3, f"{10**9 + i}.50", f"{10**9 + i}.25")
        for i in range(10)
    ),
]

#: string, integer, decimal(18, 2), double, as each engine spells them in a CAST.
TYPES = {
    "postgres": ("VARCHAR(10)", "BIGINT", "NUMERIC(18,2)", "DOUBLE PRECISION"),
    "mysql": ("CHAR", "SIGNED", "DECIMAL(18,2)", "DOUBLE"),
    "snowflake": ("VARCHAR", "BIGINT", "NUMBER(18,2)", "DOUBLE"),
    "bigquery": ("STRING", "INT64", "NUMERIC", "FLOAT64"),
    "clickhouse": ("String", "Nullable(Int64)", "Nullable(Decimal(18,2))", "Nullable(Float64)"),
    "dremio": ("VARCHAR", "BIGINT", "DECIMAL(18,2)", "DOUBLE"),
    "databricks": ("STRING", "BIGINT", "DECIMAL(18,2)", "DOUBLE"),
    "duckdb": ("VARCHAR", "BIGINT", "DECIMAL(18,2)", "DOUBLE"),
}

#: How far an engine's answer may be from the exact one, relatively. A double
#: keeps about 1e-16, a variance far from zero some digits fewer; Snowflake
#: answers the variance of a NUMBER at a fixed scale, 6 places for integers.
TOLERANCE = Fraction(1, 10**6)

#: The statistics an engine computes by subtracting sums of squares, which
#: cancel far from zero, with no stable function to render instead: Snowflake's
#: over doubles and its covariance of integers answer 0, Dremio's over doubles
#: and its covariance answer 0, NaN or a negative variance. Its variance and
#: standard deviation of integers and decimals are computed exactly instead
#: (``Dialect.exact_variance``). Asserted apart, as failing.
CANCELLING: dict[str, set[str]] = {
    "snowflake": {f"F {a}" for a in SINGLE} | {f"XY {a}" for a in PAIRED},
    "dremio": {f"F {a}" for a in SINGLE} | {"XY covar_samp", "XY covar_pop"},
}

MODEL = """\
version: 1.0
dataObjects:
  Values:
    code: t
    columns:
      Label: {code: l, abstractType: string}
      X: {code: x, abstractType: int}
      Y: {code: y, abstractType: int}
      D: {code: d, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      F: {code: f, abstractType: float}
dimensions:
  Label: {dataObject: Values, column: Label, resultType: string}
measures:
"""


def _model() -> str:
    def measure(name: str, columns: str, aggregation: str) -> str:
        refs = ", ".join(f"{{dataObject: Values, column: {c}}}" for c in columns)
        return f"  {name}: {{columns: [{refs}], aggregation: {aggregation}, resultType: float}}"

    lines = [measure(f"{c} {a}", c, a) for c in "XDF" for a in SINGLE]
    lines += [measure(f"XY {a}", "XY", a) for a in PAIRED]
    return MODEL + "\n".join(lines) + "\n"


def _rows(dialect: str) -> str:
    """``WITH t AS (...)``: the rows, as a table of this engine."""
    text, whole, fixed, real = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST({'NULL' if v is None else repr(str(v))} AS {kind})"

    rows = " UNION ALL ".join(
        f"SELECT {value(label, text)} AS {q('l')}, {value(x, whole)} AS {q('x')}, "
        f"{value(y, whole)} AS {q('y')}, {value(d, fixed)} AS {q('d')}, "
        f"{value(f, real)} AS {q('f')}"
        for label, x, y, d, f in ROWS
    )
    return f"WITH {q('t')} AS ({rows})\n"


def _exact(label: str, columns: str, aggregation: str) -> Fraction | None:
    """The statistic of *label*'s rows, exactly; ``None`` where it is undefined.

    A standard deviation and a correlation are irrational, so their square is
    returned, signed as the correlation is. ``regr_*(a, b)`` regresses the
    first column on the second, as the SQL functions do.
    """
    index = {"X": 1, "Y": 2, "D": 3, "F": 4}
    pairs = [
        (Fraction(str(row[index[columns[0]]])), Fraction(str(row[index[columns[-1]]])))
        for row in ROWS
        if row[0] == label
        and row[index[columns[0]]] is not None
        and row[index[columns[-1]]] is not None
    ]
    n = len(pairs)
    population = aggregation in ("stddev_pop", "var_pop", "covar_pop") or aggregation.startswith(
        "regr"
    )
    if n == 0 or (not population and n < 2):
        return None
    mean_a = sum(a for a, _ in pairs) / n
    mean_b = sum(b for _, b in pairs) / n
    products = sum((a - mean_a) * (b - mean_b) for a, b in pairs)
    squares_a = sum((a - mean_a) ** 2 for a, _ in pairs)
    squares_b = sum((b - mean_b) ** 2 for _, b in pairs)
    if aggregation == "corr":
        if squares_a == 0 or squares_b == 0:
            return None
        return products * abs(products) / (squares_a * squares_b)
    if aggregation.startswith("regr"):
        if squares_b == 0:
            return None
        slope = products / squares_b
        return slope if aggregation == "regr_slope" else mean_a - slope * mean_b
    return products / (n if population else n - 1)


def _measures(dialect: str) -> list[str]:
    unsupported = DialectRegistry.get(dialect).capabilities.unsupported_aggregations
    return [f"{c} {a}" for c in "XDF" for a in SINGLE] + [
        f"XY {a}" for a in PAIRED if a not in unsupported
    ]


def _mismatches(target: VendorTarget, *, cancelling: bool = False) -> list[str]:
    """Each answer that is not the exact statistic, as ``label measure: answer``.

    Far from zero, the statistics :data:`CANCELLING` names for the engine are
    left out, or with *cancelling* are the only ones compared.
    """
    store = ModelStore()
    measures = _measures(target.dialect)
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": measures}})
    sql = store.compile_query(store.load_model(_model()).model_id, query, target.dialect).sql
    found = []
    for raw in target.execute(_rows(target.dialect) + sql):
        row = {key.lower(): value for key, value in raw.items()}
        for measure in measures:
            known = row["label"] == "far" and measure in CANCELLING.get(target.dialect, set())
            if known != cancelling:
                continue
            columns, aggregation = measure.split()
            got: Any = row[measure.lower()]
            exact = _exact(row["label"], columns, aggregation)
            wrong = f"{row['label']} {measure}: {got}"
            if got is None or (isinstance(got, float) and not math.isfinite(got)):
                if got is not None or exact is not None:
                    found.append(wrong)
                continue
            if exact is None:
                found.append(wrong)
                continue
            value = Fraction(Decimal(str(got)))
            if aggregation.startswith("stddev") or aggregation == "corr":
                value *= abs(value)
            if abs(value - exact) > TOLERANCE * abs(exact):
                found.append(wrong)
    return found


def test_duckdb_statistics(vendor_duckdb: VendorTarget) -> None:
    assert _mismatches(vendor_duckdb) == []


def test_postgres_statistics(vendor_postgres: VendorTarget) -> None:
    assert _mismatches(vendor_postgres) == []


def test_mysql_statistics(vendor_mysql: VendorTarget) -> None:
    assert _mismatches(vendor_mysql) == []


def test_clickhouse_statistics(vendor_clickhouse: VendorTarget) -> None:
    assert _mismatches(vendor_clickhouse) == []


def test_snowflake_statistics(vendor_snowflake: VendorTarget) -> None:
    assert _mismatches(vendor_snowflake) == []


@pytest.mark.xfail(strict=True, reason="Snowflake cancels sums of squares of doubles")
def test_snowflake_statistics_far_from_zero(vendor_snowflake: VendorTarget) -> None:
    assert _mismatches(vendor_snowflake, cancelling=True) == []


def test_bigquery_statistics(vendor_bigquery: VendorTarget) -> None:
    assert _mismatches(vendor_bigquery) == []


def test_databricks_statistics(vendor_databricks: VendorTarget) -> None:
    assert _mismatches(vendor_databricks) == []


def test_dremio_statistics(vendor_dremio: VendorTarget) -> None:
    assert _mismatches(vendor_dremio) == []


@pytest.mark.xfail(strict=True, reason="Dremio cancels sums of squares")
def test_dremio_statistics_far_from_zero(vendor_dremio: VendorTarget) -> None:
    assert _mismatches(vendor_dremio, cancelling=True) == []
