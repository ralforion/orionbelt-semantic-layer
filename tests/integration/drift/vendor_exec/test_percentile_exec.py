"""``percentile_cont`` and ``percentile_disc`` compiled and run on each engine.

Five engines have both as ordered-set aggregates and agree on them; BigQuery
has them only as window functions, MySQL not at all, and ClickHouse's exact
quantiles are not ``PERCENTILE_DISC`` (2, 4 and 10 at 0.1, 0.3 and 0.9 of
1..10, where it is 1, 3 and 9). So the same answers, worked out in Python,
are asserted on all of them: at fractions whose position a double gets wrong
(``0.3 * 10`` is 3.0000000000000004), over NULLs, a group of NULLs, decimals
at their last digit, an integer past a double's 2^53 (exact for the discrete
percentile wherever the engine keeps the column's type), INT64 values of
opposite signs whose difference overflows, under a measure filter, inside a
derived metric, and over no rows.
"""

from __future__ import annotations

import contextlib
import math
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.ast.nodes import ColumnRef, FunctionCall
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from ._seed import SCHEMA as SEED_SCHEMA
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

TABLE = "percentile_values"

SCHEMAS = {
    "bigquery": SEED_SCHEMA,
    "snowflake": SEED_SCHEMA,
    "databricks": SEED_SCHEMA,
    "dremio": "$scratch",
}

#: (string, bigint, decimal(20, 9), double) as each engine spells them in a CAST.
TYPES: dict[str, tuple[str, str, str, str]] = {
    "duckdb": ("VARCHAR", "BIGINT", "DECIMAL(20, 9)", "DOUBLE"),
    "postgres": ("TEXT", "BIGINT", "NUMERIC(20, 9)", "DOUBLE PRECISION"),
    "mysql": ("CHAR(8)", "SIGNED", "DECIMAL(20, 9)", "DOUBLE"),
    "clickhouse": (
        "Nullable(String)",
        "Nullable(Int64)",
        "Nullable(Decimal(20, 9))",
        "Nullable(Float64)",
    ),
    "snowflake": ("VARCHAR", "BIGINT", "NUMBER(20, 9)", "DOUBLE"),
    "bigquery": ("STRING", "INT64", "NUMERIC", "FLOAT64"),
    "databricks": ("STRING", "BIGINT", "DECIMAL(20, 9)", "DOUBLE"),
    "dremio": ("VARCHAR", "BIGINT", "DECIMAL(20, 9)", "DOUBLE"),
}

#: Engines whose continuous percentile of an integer is not a double.
EXACT_CONT = {"bigquery"}
#: Engines whose discrete percentile is a value of the column's own type; MySQL
#: reads it back from a string as a DOUBLE, and Dremio's PERCENTILE_DISC is one.
EXACT_DISC = {"bigquery", "clickhouse", "databricks", "duckdb", "postgres", "snowflake"}

#: (group, integer, decimal, double)
ROWS: list[tuple[str, int | None, str | None, str | None]] = [
    *(("T", n, None, None) for n in range(1, 11)),
    ("N", None, None, None),
    ("N", 4, None, None),
    ("N", None, None, None),
    ("N", 2, None, None),
    ("E", None, None, None),
    # Past a double's 2^53: 9007199254740992 as one.
    ("B", 9007199254740993, None, None),
    # Opposite signs whose difference overflows INT64.
    ("X", -9223372036854775807, None, None),
    ("X", 9223372036854775807, None, None),
    # Decimals at their last digit: 0.3 of them is 0.0000000016, between two.
    ("D", None, "0.000000001", None),
    ("D", None, "0.000000002", None),
    ("D", None, "0.000000003", None),
    # Near a double's limit: the weight multiplied in before the division
    # overflowed MySQL's DOUBLE, though 0.9 of the way between them fits.
    ("W", None, None, "1e307"),
    ("W", None, None, "2e307"),
]

#: (measure, column index in ROWS, aggregation, fraction)
MEASURES: list[tuple[str, int, str, str]] = [
    ("Int P30 Cont", 1, "percentile_cont", "0.3"),
    ("Int P30 Disc", 1, "percentile_disc", "0.3"),
    ("Int P90 Cont", 1, "percentile_cont", "0.9"),
    ("Int P90 Disc", 1, "percentile_disc", "0.9"),
    ("Int Tiny Cont", 1, "percentile_cont", "0.00001"),
    ("Int Tiny Disc", 1, "percentile_disc", "0.00001"),
    ("Dec P30 Cont", 2, "percentile_cont", "0.3"),
    ("Dec P30 Disc", 2, "percentile_disc", "0.3"),
    ("Dbl P90 Cont", 3, "percentile_cont", "0.9"),
]

MODEL_YAML = """
version: "1.0"
name: percentile_vendor
dataObjects:
  Values:
    code: {table}
    schema: '{schema}'
    columns:
      Group: {{code: grp, abstractType: string}}
      Integer: {{code: int_val, abstractType: int}}
      Decimal: {{code: dec_val, abstractType: float}}
      Double: {{code: dbl_val, abstractType: float}}
dimensions:
  Group: {{dataObject: Values, column: Group, resultType: string}}
measures:
{measures}
  Big P90 Disc:
    columns: [{{dataObject: Values, column: Integer}}]
    aggregation: percentile_disc
    percentile: 0.9
    filters:
      - column: {{dataObject: Values, column: Group}}
        operator: equals
        values: [{{dataType: string, valueString: B}}]
metrics:
  P30 Doubled:
    expression: '{{[Int P30 Cont]}} * 2'
  Hundred Minus P30:
    expression: '100 - {{[Int P30 Disc]}}'
"""


def _measures_yaml() -> str:
    column = {1: "Integer", 2: "Decimal", 3: "Double"}
    return "\n".join(
        f"  {name}:\n"
        f"    columns: [{{dataObject: Values, column: {column[index]}}}]\n"
        f"    aggregation: {aggregation}\n"
        f"    percentile: {fraction}"
        for name, index, aggregation, fraction in MEASURES
    )


def _literal(value: str | int | None, type_name: str) -> str:
    text = "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)
    return f"CAST({text} AS {type_name})"


def _prepare(target: VendorTarget) -> SemanticModel:
    dialect = DialectRegistry.get(target.dialect)
    schema = SCHEMAS.get(target.dialect)
    ref = dialect.quote_identifier(TABLE)
    if schema:
        ref = f"{dialect.quote_identifier(schema)}.{ref}"
    names = ("grp", "int_val", "dec_val", "dbl_val")
    columns = list(zip(names, TYPES[target.dialect], strict=True))
    legs = " UNION ALL ".join(
        "SELECT "
        + ", ".join(
            f"{_literal(value, type_name)} AS {dialect.quote_identifier(name)}"
            for value, (name, type_name) in zip(row, columns, strict=True)
        )
        for row in ROWS
    )
    engine = " ENGINE = Memory" if target.dialect == "clickhouse" else ""
    for statement in (f"DROP TABLE IF EXISTS {ref}", f"CREATE TABLE {ref}{engine} AS {legs}"):
        # DDL returns no cursor description for the fixture to read.
        with contextlib.suppress(TypeError):
            target.execute(statement)
    yaml_text = MODEL_YAML.format(table=TABLE, schema=schema or "", measures=_measures_yaml())
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


def _percentile(aggregation: str, values: list[Decimal], fraction: Decimal) -> Decimal | None:
    """The SQL-standard percentile of *values*, worked out in decimals."""
    if not values:
        return None
    ordered = sorted(values)
    if aggregation == "percentile_disc":
        return ordered[max(math.ceil(fraction * len(ordered)), 1) - 1]
    position = fraction * (len(ordered) - 1)
    lower = ordered[math.floor(position)]
    upper = ordered[math.ceil(position)]
    return lower + (position - math.floor(position)) * (upper - lower)


def _want(group: str, index: int, aggregation: str, fraction: str) -> Decimal | None:
    values = [
        Decimal(str(row[index])) for row in ROWS if row[0] == group and row[index] is not None
    ]
    return _percentile(aggregation, values, Decimal(fraction))


def _close(got: Decimal | None, want: Decimal | None) -> bool:
    """Equal, but for a double's last bits."""
    if got is None or want is None:
        return got is None and want is None
    return abs(got - want) <= abs(want) * Decimal("1e-12")


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    # The positions a double gets wrong, and the decimal between two values.
    assert _want("T", 1, "percentile_disc", "0.3") == 3
    assert _want("T", 1, "percentile_cont", "0.3") == Decimal("3.7")
    assert _want("D", 2, "percentile_cont", "0.3") == Decimal("0.0000000016")
    assert _want("W", 3, "percentile_cont", "0.9") == Decimal("1.9e307")

    names = [name for name, *_ in MEASURES]
    rows = _run(target, model, ["Group"], names)
    got = {r["group"]: {n: _number(r[n.lower()]) for n in names} for r in rows}
    groups = sorted({row[0] for row in ROWS})
    assert sorted(got) == groups, f"{target.name}: {sorted(got)}"
    diff = {
        (group, name): (got[group][name], want)
        for group in groups
        for name, index, aggregation, fraction in MEASURES
        if not _close(got[group][name], want := _want(group, index, aggregation, fraction))
    }
    assert not diff, f"{target.name}: {diff}"
    if target.dialect in EXACT_CONT:
        assert got["B"]["Int P90 Cont"] == 9007199254740993, f"{target.name}: {got['B']}"
        assert got["X"]["Int P30 Cont"] == Decimal("-3689348814741910322.8"), target.name
    if target.dialect in EXACT_DISC:
        assert got["B"]["Int P90 Disc"] == 9007199254740993, f"{target.name}: {got['B']}"

    # Under a measure filter, whose CASE carries no column type.
    rows = _run(target, model, [], ["Big P90 Disc"])
    got_filtered = [_number(r["big p90 disc"]) for r in rows]
    assert _close(got_filtered[0], Decimal(9007199254740993)), f"{target.name}: {got_filtered}"
    if target.dialect in EXACT_DISC:
        assert got_filtered == [9007199254740993], f"{target.name}: {got_filtered}"

    # Inside a formula the percentile is one operand.
    derived = ["P30 Doubled", "Hundred Minus P30"]
    t_only = [QueryFilter(field="Group", op="=", value="T")]
    rows = _run(target, model, ["Group"], derived, t_only)
    got_derived = [tuple(_number(r[m.lower()]) for m in derived) for r in rows]
    assert len(got_derived) == 1, f"{target.name}: {got_derived}"
    assert _close(got_derived[0][0], Decimal("7.4")), f"{target.name}: {got_derived}"
    assert got_derived[0][1] == 97, f"{target.name}: {got_derived}"

    # No rows at all: NULL. Dremio returns no row for an ungrouped continuous
    # percentile over no rows, as for its median; its discrete one is a row.
    nothing = [QueryFilter(field="Group", op="=", value="none")]
    rows = _run(target, model, [], ["Int P30 Disc"], nothing)
    assert [r["int p30 disc"] for r in rows] == [None], f"{target.name}: {rows}"
    rows = _run(target, model, [], ["Int P30 Cont"], nothing)
    want_none = [] if target.dialect == "dremio" else [None]
    assert [r["int p30 cont"] for r in rows] == want_none, f"{target.name}: {rows}"


#: Two BIGNUMERIC values at the last of their 38 places, and the exact
#: percentile between them rounded once, half away from zero, as an engine's
#: mean is: -2e-38 for -1.5e-38. One step from the wrong end, or two products
#: added, round the other way.
_BIGNUMERIC_CASES = [
    ("-2e-38", "-1e-38", "0.5", "-2e-38"),
    ("-1e-38", "2e-38", "0.5", "1e-38"),
    ("-3e-38", "2e-38", "0.3", "-2e-38"),
    ("-3e-38", "2e-38", "0.7", "1e-38"),
    ("-7e-38", "3e-38", "0.25", "-5e-38"),
    ("1e-38", "2e-38", "0.5", "2e-38"),
    # Past an INT64 difference: one rounding still, not two products added.
    (
        "-10000000000000000000.00000000000000000000000000000000000001",
        "10000000000000000000.00000000000000000000000000000000000002",
        "0.5",
        "1e-38",
    ),
]


def test_bigquery_bignumeric_rounds_once(vendor_bigquery: VendorTarget) -> None:
    dialect = DialectRegistry.get("bigquery")
    for lower, upper, fraction, want in _BIGNUMERIC_CASES:
        call = FunctionCall(
            name="PERCENTILE_CONT", args=[ColumnRef(name="x")], fraction=Decimal(fraction)
        )
        values = f"UNNEST([BIGNUMERIC '{lower}', BIGNUMERIC '{upper}']) AS x"
        rows = vendor_bigquery.execute(f"SELECT {dialect.compile_expr(call)} AS v FROM {values}")
        got = Decimal(str(rows[0]["v"]))
        assert got == Decimal(want), (lower, upper, fraction, got)


def test_duckdb_percentile(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_percentile(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_percentile(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_percentile(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_snowflake_percentile(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_bigquery_percentile(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_databricks_percentile(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_percentile(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
