"""Dremio's variance and standard deviation of integers and decimals, exact.

Dremio's own subtract sums of squares as doubles and cancel far from zero
(``test_statistics_exec``); ``Dialect.exact_variance`` computes them from
exact moments instead. Seeded random groups - far from zero, negative, of one
value, constant, with NULLs, and near the limit past which Dremio's own is
kept - are each held to the exact statistic of their values, within the few
roundings to double the route makes. A group past the limit answers what
Dremio's own statistic answers.
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
