"""Variances, covariances, correlations and regressions of integers and decimals, exact.

Dremio's variance and covariance and Snowflake's covariance, correlation and
regression subtract sums of squares or products as doubles and cancel far from
zero (``test_statistics_exec``); PostgreSQL's covariance family and MySQL's
variance read a value past a double's 53 bits as a double, and lose its last
digits. ``Dialect.exact_statistic`` computes them from exact moments instead.
Seeded random groups - far from zero, negative, of one value, constant, with
NULLs, and near the limit past which the engine's own is kept - are each held
to the exact statistic of their values, within the few
roundings to double the route makes. A group past the limit answers what the
engine's own statistic answers.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal
from fractions import Fraction

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

#: Each column's type and places: an integer and two declared decimals.
COLUMNS = {"X": ("BIGINT", 0), "D": ("DECIMAL(18, 2)", 2), "E": ("DECIMAL(24, 6)", 6)}
AGGREGATIONS = ("variance", "var_pop", "stddev", "stddev_pop")

#: Built in Dremio's scratch space: Dremio plans inline rows slowly, measured
#: at 24 s for these measures over 120 rows, 0.7 s over the table.
TABLE = '"$scratch"."variance_values"'

MODEL = """\
version: 1.0
dataObjects:
  Values:
    code: variance_values
    schema: $scratch
    columns:
      Label: {code: l, abstractType: string}
      X: {code: x, abstractType: int}
      D: {code: d, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      E: {code: e, abstractType: decimal, sqlPrecision: 24, sqlScale: 6}
dimensions:
  Label: {dataObject: Values, column: Label, resultType: string}
measures:
"""

#: A group's values, by column, in units of the column's last place.
Group = dict[str, list[int | None]]


def _groups() -> dict[str, Group]:
    """Random groups, every value within the limit, then one near it and one past it.

    A value is ``centre + offset`` units: at most 10^17 + 10^6 of them, whose
    squares, 40 to a group, sum to under 10^36.
    """
    rng = random.Random(689)
    groups: dict[str, Group] = {}
    for g in range(40):
        size = rng.choice([1, 2, 3, 10, 40])
        group: Group = {}
        for column in COLUMNS:
            centre = rng.choice([0, 10, 10**3, 10**9, 10**12, 10**15, 10**17])
            centre *= rng.choice([1, -1])
            spread = rng.choice([0, 1, 7, 1000, 10**6])
            values: list[int | None] = [centre + rng.randint(-spread, spread) for _ in range(size)]
            if size > 2 and rng.random() < 0.3:
                values[rng.randrange(size)] = None
            group[column] = values
        groups[f"g{g:02d}"] = group
    # Sums of squares of 0.98 and 3.2 times 10^37 units: below the limit and past it.
    for label, magnitude, size in (("near", 99 * 10**16, 10), ("past", 9 * 10**17, 40)):
        groups[label] = {column: [magnitude + i for i in range(size)] for column in COLUMNS}
    return groups


def _literal(units: int | None, column: str) -> str:
    kind, places = COLUMNS[column]
    if units is None:
        return f"CAST(NULL AS {kind})"
    value = Decimal(units).scaleb(-places)
    return f"CAST('{value:f}' AS {kind})"


def _create(target: VendorTarget, groups: dict[str, Group]) -> None:
    rows = ", ".join(
        f"(CAST('{label}' AS VARCHAR), {', '.join(_literal(group[c][i], c) for c in COLUMNS)})"
        for label, group in groups.items()
        for i in range(len(group["X"]))
    )
    target.execute(f"DROP TABLE IF EXISTS {TABLE}")
    target.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM (VALUES {rows}) AS v(l, x, d, e)")


def _exact(values: list[int | None], places: int, aggregation: str) -> Fraction | None:
    """The variance of *values*, or a deviation's square; ``None`` where undefined."""
    present = [Fraction(v, 10**places) for v in values if v is not None]
    population = aggregation.endswith("_pop")
    if not present or (not population and len(present) < 2):
        return None
    mean = sum(present) / len(present)
    spread = sum((v - mean) ** 2 for v in present)
    return spread / (len(present) if population else len(present) - 1)


def _model() -> str:
    lines = [
        f"  {c} {a}: {{columns: [{{dataObject: Values, column: {c}}}], "
        f"aggregation: {a}, resultType: float}}"
        for c in COLUMNS
        for a in AGGREGATIONS
    ]
    return MODEL + "\n".join(lines) + "\n"


def test_dremio_exact_variance(vendor_dremio: VendorTarget) -> None:
    groups = _groups()
    _create(vendor_dremio, groups)
    measures = [f"{c} {a}" for c in COLUMNS for a in AGGREGATIONS]
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": measures}})
    sql = store.compile_query(store.load_model(_model()).model_id, query, "dremio").sql
    rows = {row["Label"]: row for row in vendor_dremio.execute(sql)}
    assert set(rows) == set(groups)

    own = ", ".join(
        f'{a.upper()}("{c.lower()}") AS "{c} {a}"' for c in COLUMNS for a in AGGREGATIONS
    )
    native = vendor_dremio.execute(f"SELECT {own} FROM {TABLE} WHERE \"l\" = 'past'")[0]

    wrong = []
    for label, group in groups.items():
        for measure in measures:
            column, aggregation = measure.split()
            got = rows[label][measure]
            if label == "past":
                own = native[measure]
                if got != own and not (math.isnan(got) and math.isnan(own)):
                    wrong.append(f"{label} {measure}: {got}, Dremio's own {own}")
                continue
            exact = _exact(group[column], COLUMNS[column][1], aggregation)
            if got is not None and not math.isfinite(got):
                wrong.append(f"{label} {measure}: {got}")
                continue
            if exact is None or got is None:
                if exact is not None or got is not None:
                    wrong.append(f"{label} {measure}: {got}, exact {exact}")
                continue
            value = Fraction(got)
            if aggregation.startswith("stddev"):
                value *= value
            if abs(value - exact) > abs(exact) / 10**12:
                wrong.append(f"{label} {measure}: {got}, exact {float(exact)}")
    assert wrong == []


@pytest.mark.parametrize("scale", [37, 38])
def test_dremio_variance_at_the_most_places(vendor_dremio: VendorTarget, scale: int) -> None:
    """Exact at 37 places; at 38, whose multiplier Dremio cannot hold, its own."""
    model = f"""\
version: 1.0
dataObjects:
  Values:
    code: v
    columns:
      W: {{code: w, abstractType: decimal, sqlPrecision: 38, sqlScale: {scale}}}
measures:
  W variance:
    columns: [{{dataObject: Values, column: W}}]
    aggregation: variance
    resultType: float
"""
    rows = ", ".join(
        f"(CAST('{Decimal(n).scaleb(-scale):f}' AS DECIMAL(38, {scale})))" for n in (1, 2, 3)
    )
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"measures": ["W variance"]}})
    sql = store.compile_query(store.load_model(model).model_id, query, "dremio").sql
    got = vendor_dremio.execute(f'WITH "v" AS (SELECT * FROM (VALUES {rows}) AS t("w"))\n{sql}')

    assert math.isclose(got[0]["W variance"], 10.0 ** (-2 * scale), rel_tol=1e-12)


#: The two-column statistics each engine computes from exact moments; Dremio
#: has no ``corr`` or regression.
PAIRED = {
    "dremio": ("covar_samp", "covar_pop"),
    "snowflake": ("covar_samp", "covar_pop", "corr", "regr_slope", "regr_intercept"),
    "postgres": ("covar_samp", "covar_pop", "corr", "regr_slope", "regr_intercept"),
}
PAIRS = (("X", "D"), ("D", "E"))

PAIRED_MODEL = """\
version: 1.0
dataObjects:
  Values:
    code: paired_values
    columns:
      Label: {code: l, abstractType: string}
      X: {code: x, abstractType: int}
      D: {code: d, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      E: {code: e, abstractType: decimal, sqlPrecision: 24, sqlScale: 6}
dimensions:
  Label: {dataObject: Values, column: Label, resultType: string}
measures:
"""


def _paired_groups() -> dict[str, Group]:
    """Random groups under Snowflake's bound of 10^13 units, a NULL on either
    side of some pairs, and a constant column in some."""
    rng = random.Random(691)
    groups: dict[str, Group] = {}
    for g in range(30):
        size = rng.choice([1, 2, 3, 10, 40])
        group: Group = {}
        for column in COLUMNS:
            centre = rng.choice([0, 10, 10**3, 10**9, 10**12]) * rng.choice([1, -1])
            spread = rng.choice([0, 1, 7, 1000])
            values: list[int | None] = [centre + rng.randint(-spread, spread) for _ in range(size)]
            if size > 2 and rng.random() < 0.3:
                values[rng.randrange(size)] = None
            group[column] = values
        groups[f"p{g:02d}"] = group
    return groups


def _paired_exact(a: list[int | None], b: list[int | None], places: tuple[int, int], name: str):
    """The statistic of the pairs, a correlation squared and signed; ``None``
    where undefined. Also the intercept's scale: its mean and slope terms."""
    pairs = [
        (Fraction(x, 10 ** places[0]), Fraction(y, 10 ** places[1]))
        for x, y in zip(a, b, strict=True)
        if x is not None and y is not None
    ]
    n = len(pairs)
    if n == 0 or (name == "covar_samp" and n < 2):
        return None, None
    mean_a = sum(x for x, _ in pairs) / n
    mean_b = sum(y for _, y in pairs) / n
    co = sum((x - mean_a) * (y - mean_b) for x, y in pairs)
    spread_a = sum((x - mean_a) ** 2 for x, _ in pairs)
    spread_b = sum((y - mean_b) ** 2 for _, y in pairs)
    if name == "covar_samp":
        return co / (n - 1), None
    if name == "covar_pop":
        return co / n, None
    if name == "corr":
        if spread_a == 0 or spread_b == 0:
            return None, None
        return co * abs(co) / (spread_a * spread_b), None
    if spread_b == 0:
        return None, None
    slope = co / spread_b
    if name == "regr_slope":
        return slope, None
    return mean_a - slope * mean_b, abs(mean_a) + abs(slope * mean_b)


def _paired_rows(dialect: str, groups: dict[str, Group]) -> str:
    """``WITH paired_values AS (VALUES ...)``, in this engine's types."""
    rows = ", ".join(
        f"(CAST('{label}' AS VARCHAR), {', '.join(_literal(group[c][i], c) for c in COLUMNS)})"
        for label, group in groups.items()
        for i in range(len(group["X"]))
    )
    return f'WITH "paired_values" AS (SELECT * FROM (VALUES {rows}) AS v("l", "x", "d", "e"))\n'


def _paired_sql(dialect: str) -> tuple[str, list[str]]:
    measures = [f"{a}{b} {name}" for a, b in PAIRS for name in PAIRED[dialect]]
    lines = [
        f"  {a}{b} {name}: {{columns: [{{dataObject: Values, column: {a}}}, "
        f"{{dataObject: Values, column: {b}}}], aggregation: {name}, resultType: float}}"
        for a, b in PAIRS
        for name in PAIRED[dialect]
    ]
    store = ModelStore()
    model_id = store.load_model(PAIRED_MODEL + "\n".join(lines) + "\n").model_id
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": measures}})
    return store.compile_query(model_id, query, dialect).sql, measures


def _assert_paired(target: VendorTarget, groups: dict[str, Group]) -> None:
    sql, measures = _paired_sql(target.dialect)
    rows = {row["Label"]: row for row in target.execute(_paired_rows(target.dialect, groups) + sql)}
    assert set(rows) == set(groups)
    wrong = []
    for label, group in groups.items():
        for measure in measures:
            pair, name = measure.split()
            a, b = pair[0], pair[1]
            exact, scale = _paired_exact(group[a], group[b], (COLUMNS[a][1], COLUMNS[b][1]), name)
            got = rows[label][measure]
            if got is not None and not math.isfinite(got):
                wrong.append(f"{label} {measure}: {got}")
                continue
            if exact is None or got is None:
                if exact is not None or got is not None:
                    wrong.append(f"{label} {measure}: {got}, exact {exact}")
                continue
            value = Fraction(got)
            if name == "corr":
                value *= abs(value)
            allowed = (scale if scale is not None else abs(exact)) / 10**12
            if abs(value - exact) > allowed:
                wrong.append(f"{label} {measure}: {got}, exact {float(exact)}")
    assert wrong == [], f"{target.name}: {wrong}"


def test_dremio_exact_covariance(vendor_dremio: VendorTarget) -> None:
    _assert_paired(vendor_dremio, _paired_groups())


def test_snowflake_exact_paired_statistics(vendor_snowflake: VendorTarget) -> None:
    _assert_paired(vendor_snowflake, _paired_groups())


#: A covariance of 18.5 far from zero, beside a value without its pair, which
#: is in no sum: read raw, it sent the group to the engine's own statistic,
#: 142.2 on Dremio and 0 on Snowflake.
UNPAIRED: dict[str, Group] = {
    "unpaired": {
        "X": [*(10**9 + i for i in range(10)), 9 * 10**18],
        "D": [*((10**9 + 2 * i + i % 3) * 100 for i in range(10)), None],
        "E": [None] * 11,
    }
}


@pytest.mark.parametrize("vendor", ["vendor_dremio", "vendor_snowflake"])
def test_a_value_without_its_pair_counts_nowhere(
    vendor: str, request: pytest.FixtureRequest
) -> None:
    _assert_paired(request.getfixturevalue(vendor), UNPAIRED)


def test_snowflake_keeps_its_own_past_25_places(vendor_snowflake: VendorTarget) -> None:
    """In units, 10^-33 at 37 places is 10^4, and its unscaled product 10^41
    passes 128 bits, which Snowflake raised on. Its own covariance answers, a
    double cancelling 8 of its digits: 1.0000000052e-74."""
    model = """\
version: 1.0
dataObjects:
  Values:
    code: deep_values
    columns:
      A: {code: a, abstractType: decimal, sqlPrecision: 38, sqlScale: 37}
measures:
  Spread:
    columns: [{dataObject: Values, column: A}, {dataObject: Values, column: A}]
    aggregation: covar_samp
    resultType: float
"""
    rows = " UNION ALL ".join(
        f"SELECT CAST('{Decimal(10**4 + i).scaleb(-37):f}' AS DECIMAL(38, 37)) AS \"a\""
        for i in range(3)
    )
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"measures": ["Spread"]}})
    sql = store.compile_query(store.load_model(model).model_id, query, "snowflake").sql
    (row,) = vendor_snowflake.execute(f'WITH "deep_values" AS ({rows})\n{sql}')

    assert math.isclose(row["Spread"], 1e-74, rel_tol=1e-7)


def test_snowflake_keeps_its_own_past_the_bound(vendor_snowflake: VendorTarget) -> None:
    """Values of 13 digits or more in units are left out of the exact sums,
    which would otherwise raise past 128 bits (40 squares of 9e18), and the
    group reads Snowflake's own statistic."""
    groups = {"big": {c: [9 * 10**18 + i for i in range(40)] for c in ("X", "D", "E")}}
    groups["big"]["D"] = [10**15 + i for i in range(40)]
    groups["big"]["E"] = [10**17 + i for i in range(40)]
    sql, measures = _paired_sql("snowflake")
    (row,) = vendor_snowflake.execute(_paired_rows("snowflake", groups) + sql)
    assert set(row) == {"Label", *measures}


#: The most units each column holds: a BIGINT near its largest, and the
#: decimals at their width.
WIDEST = {"X": 9 * 10**18, "D": 99 * 10**16, "E": 99 * 10**22}


def _wide_groups() -> dict[str, Group]:
    """Random groups out to the widest each column holds, past a double's 53
    bits, a NULL on either side of some pairs; PostgreSQL's ``NUMERIC`` and
    MySQL's ``DECIMAL(65)`` hold their sums of squares."""
    rng = random.Random(697)
    groups: dict[str, Group] = {}
    for g in range(30):
        size = rng.choice([1, 2, 3, 10, 40])
        group: Group = {}
        for column, widest in WIDEST.items():
            centre = rng.choice([10**15, 10**17, widest // 10, widest]) * rng.choice([1, -1])
            spread = rng.choice([0, 1, 7, 1000, 10**6])
            values: list[int | None] = [
                max(-widest, min(widest, centre + rng.randint(-spread, spread)))
                for _ in range(size)
            ]
            if size > 2 and rng.random() < 0.3:
                values[rng.randrange(size)] = None
            group[column] = values
        groups[f"w{g:02d}"] = group
    return groups


def test_postgres_exact_paired_statistics(vendor_postgres: VendorTarget) -> None:
    """Over 10^17 + 0..5 its own covariances were 0, its correlation NULL."""
    _assert_paired(vendor_postgres, _wide_groups())


@pytest.mark.parametrize("places", [100, 160])
def test_postgres_at_many_places(vendor_postgres: VendorTarget, places: int) -> None:
    """Exact at 100 places; past 150, whose powers of ten no double holds,
    PostgreSQL's own: at 160 the route's ``CAST('1E320' AS DOUBLE PRECISION)``
    raised, in a branch not taken too."""
    kind = f"NUMERIC({places + 2}, {places})"
    measures = {
        f"{name} S": f"{{columns: [{{dataObject: V, column: X}}, {{dataObject: V, column: Y}}], "
        f"aggregation: {name}, resultType: float}}"
        for name in ("covar_samp", "corr", "regr_slope")
    }
    model = (
        "version: 1.0\ndataObjects:\n  V:\n    code: v\n    columns:\n"
        f"      X: {{code: x, abstractType: decimal, sqlPrecision: {places + 2}, "
        f"sqlScale: {places}}}\n"
        f"      Y: {{code: y, abstractType: decimal, sqlPrecision: {places + 2}, "
        f"sqlScale: {places}}}\n"
        "measures:\n" + "".join(f"  {name}: {spec}\n" for name, spec in measures.items())
    )
    rows = " UNION ALL ".join(
        f"SELECT CAST('{v}' AS {kind}) AS \"x\", CAST('{v}' AS {kind}) AS \"y\"" for v in (1, 2, 3)
    )
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"measures": list(measures)}})
    sql = store.compile_query(store.load_model(model).model_id, query, "postgres").sql
    (row,) = vendor_postgres.execute(f'WITH "v" AS ({rows})\n{sql}')

    assert {name: row[name] for name in measures} == dict.fromkeys(measures, 1.0)


@pytest.mark.parametrize(("magnitude", "exact"), [(10**149, True), (10**200, False)])
def test_postgres_far_past_a_double(
    vendor_postgres: VendorTarget, magnitude: int, exact: bool
) -> None:
    """Exact while the sums of squares are under 10^300; past them, PostgreSQL's
    own, which squares only the spread: values near 10^200 squared as doubles
    raised before the route could keep it."""
    kind = "NUMERIC(250, 0)"
    model = (
        "version: 1.0\ndataObjects:\n  V:\n    code: v\n    columns:\n"
        "      X: {code: x, abstractType: decimal, sqlPrecision: 250, sqlScale: 0}\n"
        "      Y: {code: y, abstractType: decimal, sqlPrecision: 250, sqlScale: 0}\n"
        "measures:\n  Spread:\n"
        "    columns: [{dataObject: V, column: X}, {dataObject: V, column: Y}]\n"
        "    aggregation: covar_samp\n    resultType: float\n"
    )
    rows = " UNION ALL ".join(
        f"SELECT CAST('{magnitude + i}' AS {kind}) AS \"x\", "
        f"CAST('{magnitude + i}' AS {kind}) AS \"y\""
        for i in range(3)
    )
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"measures": ["Spread"]}})
    sql = store.compile_query(store.load_model(model).model_id, query, "postgres").sql
    (row,) = vendor_postgres.execute(f'WITH "v" AS ({rows})\n{sql}')
    (native,) = vendor_postgres.execute(
        f'WITH "v" AS ({rows})\nSELECT COVAR_SAMP("x", "y") AS "Spread" FROM "v"'
    )

    assert row["Spread"] == (1.0 if exact else native["Spread"])


def test_mysql_exact_variance(vendor_mysql: VendorTarget) -> None:
    """Over 10^17 + 0..5 its own variance was 0."""
    groups = _wide_groups()
    kinds = {"X": "SIGNED", "D": "DECIMAL(18, 2)", "E": "DECIMAL(24, 6)"}

    def literal(units: int | None, column: str) -> str:
        if units is None:
            return f"CAST(NULL AS {kinds[column]})"
        return f"CAST('{Decimal(units).scaleb(-COLUMNS[column][1]):f}' AS {kinds[column]})"

    rows = " UNION ALL ".join(
        f"SELECT CAST('{label}' AS CHAR) AS l, "
        + ", ".join(f"{literal(group[c][i], c)} AS {c.lower()}" for c in COLUMNS)
        for label, group in groups.items()
        for i in range(len(group["X"]))
    )
    measures = [f"{c} {a}" for c in COLUMNS for a in AGGREGATIONS]
    model = _model().replace("    code: variance_values\n    schema: $scratch\n", "    code: t\n")
    store = ModelStore()
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": measures}})
    sql = store.compile_query(store.load_model(model).model_id, query, "mysql").sql
    found = vendor_mysql.execute(f"WITH t AS ({rows})\n{sql}")
    rows_by_label = {row["Label"]: row for row in found}
    assert set(rows_by_label) == set(groups)

    wrong = []
    for label, group in groups.items():
        for measure in measures:
            column, aggregation = measure.split()
            got = rows_by_label[label][measure]
            exact = _exact(group[column], COLUMNS[column][1], aggregation)
            if exact is None or got is None:
                if exact is not None or got is not None:
                    wrong.append(f"{label} {measure}: {got}, exact {exact}")
                continue
            value = Fraction(got)
            if aggregation.startswith("stddev"):
                value *= value
            if abs(value - exact) > abs(exact) / 10**12:
                wrong.append(f"{label} {measure}: {got}, exact {float(exact)}")
    assert wrong == []
