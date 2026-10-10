"""``aggregation: median`` compiled and run on each of the eight engines.

The median is the exact, continuous one: the mean of the two middle values
when the count is even. Engines spell that five ways - ``MEDIAN``,
``PERCENTILE_CONT ... WITHIN GROUP``, ClickHouse's exact low and high quantile,
BigQuery's sorted array, MySQL's sorted ``GROUP_CONCAT`` - and the ones they
offer under the plain name disagree: Postgres' ``PERCENTILE_DISC`` takes the
lower middle value, BigQuery's ``APPROX_QUANTILES`` approximates, ClickHouse
has no upper-case ``MEDIAN``. So the same hand-computed answers are asserted on
all of them, for integers and decimals, an even and an odd group, a group of
NULLs, and a group large enough that MySQL's ``GROUP_CONCAT`` would be cut at
its default 1024 bytes without the statement hint the dialect adds. The
``listagg`` over that group is the same cut, fixed by the same hint. At the
edges: an integer whose double would overflow INT64 (BigQuery), doubles past a
fixed decimal's range (MySQL), and no rows at all over a non-nullable column
(``NaN`` on ClickHouse).
"""

from __future__ import annotations

import contextlib
import statistics
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from ._seed import SCHEMA as SEED_SCHEMA
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

TABLE = "median_values"

SCHEMAS = {
    "bigquery": SEED_SCHEMA,
    "snowflake": SEED_SCHEMA,
    "databricks": SEED_SCHEMA,
    "dremio": "$scratch",
}

#: (string, bigint, decimal(20, 9), double, bigint never NULL) as each engine
#: spells them in a CAST. The last is non-nullable on ClickHouse, the one
#: engine whose column types say so.
TYPES: dict[str, tuple[str, str, str, str, str]] = {
    "duckdb": ("VARCHAR", "BIGINT", "DECIMAL(20, 9)", "DOUBLE", "BIGINT"),
    "postgres": ("TEXT", "BIGINT", "NUMERIC(20, 9)", "DOUBLE PRECISION", "BIGINT"),
    "mysql": ("CHAR(8)", "SIGNED", "DECIMAL(20, 9)", "DOUBLE", "SIGNED"),
    "clickhouse": (
        "Nullable(String)",
        "Nullable(Int64)",
        "Nullable(Decimal(20, 9))",
        "Nullable(Float64)",
        "Int64",
    ),
    "snowflake": ("VARCHAR", "BIGINT", "NUMBER(20, 9)", "DOUBLE", "BIGINT"),
    "bigquery": ("STRING", "INT64", "NUMERIC", "FLOAT64", "INT64"),
    "databricks": ("STRING", "BIGINT", "DECIMAL(20, 9)", "DOUBLE", "BIGINT"),
    "dremio": ("VARCHAR", "BIGINT", "DECIMAL(20, 9)", "DOUBLE", "BIGINT"),
}

#: (group, integer, decimal, label, double). Every row also has a sequence
#: number, the never-NULL column.
ROWS: list[tuple[str, int | None, str | None, str | None, str | None]] = [
    ("A", 1, "1.25", None, None),
    ("A", 2, "2.75", None, None),
    ("A", 10, "10.10", None, None),
    ("A", 20, "20.20", None, None),
    ("A", None, None, None, None),
    ("B", 1, "1.00", None, None),
    ("B", 2, "2.00", None, None),
    ("B", 10, "3.50", None, None),
    ("C", None, None, None, None),
    *(("L", n, None, f"lbl{n:05d}", None) for n in range(1, 201)),
    # Added to itself, this overflows INT64.
    ("H", 5000000000000000000, None, None, None),
    # Outside DECIMAL(65, 30), both ways.
    ("F", None, None, None, "1e40"),
    ("G", None, None, None, "1e-40"),
    # A decimal at its last digit: halving it rounds, so the median of one
    # value must not be the sum of two halves, nor of two the mean of halves.
    ("N", None, "0.000000001", None, None),
    ("M", None, "0.000000001", None, None),
    ("M", None, "0.000000003", None, None),
]

MODEL_YAML = """
version: "1.0"
name: median_vendor
dataObjects:
  Values:
    code: {table}
    schema: '{schema}'
    columns:
      Group: {{code: grp, abstractType: string}}
      Integer: {{code: int_val, abstractType: int}}
      Decimal: {{code: dec_val, abstractType: float}}
      Label: {{code: label, abstractType: string}}
      Double: {{code: dbl_val, abstractType: float}}
      Sequence: {{code: seq, abstractType: int}}
dimensions:
  Group: {{dataObject: Values, column: Group, resultType: string}}
measures:
  Integer Median:
    columns: [{{dataObject: Values, column: Integer}}]
    aggregation: median
  Decimal Median:
    columns: [{{dataObject: Values, column: Decimal}}]
    aggregation: median
  Double Median:
    columns: [{{dataObject: Values, column: Double}}]
    aggregation: median
  Sequence Median:
    columns: [{{dataObject: Values, column: Sequence}}]
    aggregation: median
  Integer Total:
    columns: [{{dataObject: Values, column: Sequence}}]
    aggregation: sum
    total: true
  Labels:
    columns: [{{dataObject: Values, column: Label}}]
    aggregation: listagg
    delimiter: ","
metrics:
  Median Doubled:
    expression: '{{[Integer Median]}} * 2'
  Hundred Minus Median:
    expression: '100 - {{[Integer Median]}}'
"""


def _literal(value: str | int | None, type_name: str) -> str:
    text = "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)
    return f"CAST({text} AS {type_name})"


def _prepare(target: VendorTarget) -> SemanticModel:
    dialect = DialectRegistry.get(target.dialect)
    text, integer, decimal, double, never_null = TYPES[target.dialect]
    schema = SCHEMAS.get(target.dialect)
    ref = dialect.quote_identifier(TABLE)
    if schema:
        ref = f"{dialect.quote_identifier(schema)}.{ref}"
    columns = (
        ("grp", text),
        ("int_val", integer),
        ("dec_val", decimal),
        ("label", text),
        ("dbl_val", double),
        ("seq", never_null),
    )
    legs = " UNION ALL ".join(
        "SELECT "
        + ", ".join(
            f"{_literal(value, type_name)} AS {dialect.quote_identifier(name)}"
            for value, (name, type_name) in zip((*row, seq), columns, strict=True)
        )
        for seq, row in enumerate(ROWS, start=1)
    )
    engine = " ENGINE = Memory" if target.dialect == "clickhouse" else ""
    for statement in (f"DROP TABLE IF EXISTS {ref}", f"CREATE TABLE {ref}{engine} AS {legs}"):
        # DDL returns no cursor description for the fixture to read.
        with contextlib.suppress(TypeError):
            target.execute(statement)
    yaml_text = MODEL_YAML.format(table=TABLE, schema=schema or "")
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _run(
    target: VendorTarget,
    model: SemanticModel,
    dimensions: list[str],
    measures: list[str],
    where: list[QueryFilter] | None = None,
) -> list[dict[str, Any]]:
    query = QueryObject(
        select=QuerySelect(dimensions=dimensions, measures=measures), where=where or []
    )
    rows = target.execute(CompilationPipeline().compile(query, model, target.dialect).sql)
    return [{str(k).lower(): v for k, v in row.items()} for row in rows]


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _close(got: tuple[Decimal | None, ...], want: tuple[Decimal | None, ...]) -> bool:
    """Equal, but for a double's last bits: several engines compute the median
    as a double, where 0.000000001 / 2 + 0.000000003 / 2 is 1.9999999999999997e-9."""
    return all(
        (g is None and w is None)
        or (g is not None and w is not None and abs(g - w) <= abs(w) * Decimal("1e-12"))
        for g, w in zip(got, want, strict=True)
    )


def _median(column: int, group: str | None = None) -> Decimal | None:
    """The median of *column* of ``ROWS`` (in *group*), as Python works it out."""
    values = [
        Decimal(str(row[column]))
        for row in ROWS
        if row[column] is not None and group in (None, row[0])
    ]
    return statistics.median(values) if values else None


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    groups = sorted({row[0] for row in ROWS})
    want = {g: (_median(1, g), _median(2, g), _median(4, g)) for g in groups}
    # Even, odd, all-NULL, a group of 200 past GROUP_CONCAT's default cut, and
    # the edges of INT64 and of a fixed decimal.
    assert want["A"][:2] == (Decimal(6), Decimal("6.425"))
    assert want["L"][0] == Decimal("100.5")
    assert want["H"][0] == Decimal(5000000000000000000)
    assert (want["F"][2], want["G"][2]) == (Decimal("1e40"), Decimal("1e-40"))
    assert (want["N"][1], want["M"][1]) == (Decimal("0.000000001"), Decimal("0.000000002"))

    measures = ["Integer Median", "Decimal Median", "Double Median"]
    for extra in ([], ["Integer Total"]):
        # Beside a total, the medians are computed in a CTE the window reads.
        rows = _run(target, model, ["Group"], [*measures, *extra])
        got = {r["group"]: tuple(_number(r[m.lower()]) for m in measures) for r in rows}
        assert got.keys() == want.keys(), f"{target.name} {extra}: {got}"
        diff = {g: (got[g], want[g]) for g in want if not _close(got[g], want[g])}
        assert not diff, f"{target.name} {extra}: {diff}"

    exact = ["Integer Median", "Decimal Median"]
    rows = _run(target, model, [], exact)
    got_all = [tuple(_number(r[m.lower()]) for m in exact) for r in rows]
    assert got_all == [(_median(1), _median(2))], f"{target.name}: {got_all}"

    # Inside a formula the median is one operand, not the last term of a sum.
    derived = ["Median Doubled", "Hundred Minus Median"]
    rows = _run(
        target, model, ["Group"], derived, [QueryFilter(field="Group", op="in", value=["A", "B"])]
    )
    got_derived = {r["group"]: tuple(_number(r[m.lower()]) for m in derived) for r in rows}
    assert got_derived["A"] == (Decimal(12), Decimal(94)), f"{target.name}: {got_derived}"
    assert got_derived["B"] == (Decimal(4), Decimal(98)), f"{target.name}: {got_derived}"

    # No rows at all: NULL, also over a column that cannot hold one.
    every = [*measures, "Sequence Median"]
    nothing = [QueryFilter(field="Group", op="=", value="none")]
    rows = _run(target, model, [], every, nothing)
    got_none = [tuple(r[m.lower()] for m in every) for r in rows]
    # Dremio returns no row at all for an ungrouped query whose median reads no
    # rows, whatever else it selects (probed: MEDIAN and PERCENTILE_CONT alike,
    # beside a COUNT(*)), where every other engine returns one row of NULLs.
    want_none = [] if target.dialect == "dremio" else [(None,) * len(every)]
    assert got_none == want_none, f"{target.name}: {got_none}"

    labels = {r["group"]: r["labels"] for r in _run(target, model, ["Group"], ["Labels"])}
    assert len(labels["L"]) == 200 * 8 + 199, f"{target.name}: {len(labels['L'])}"


def test_duckdb_median(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_median(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_median(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_median(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_snowflake_median(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_bigquery_median(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_databricks_median(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_median(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
