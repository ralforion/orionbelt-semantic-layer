"""``aggregation: first`` / ``last`` compiled and run on each engine.

The value at the least / greatest ``withinGroup`` key of each group. The
vendors' own ``first`` / ``last`` (DuckDB, Databricks, ClickHouse's ``any``)
take the first row met, and their ordered ``MAX_BY`` / ``arg_max`` break a tie
on the key arbitrarily, so each rendering orders by the (key, value) pair: a
tie goes to the greatest value for ``last`` and the least for ``first``. Rows
without a value or a key are skipped. The same answers are asserted on seven
engines, for integers past a double's 2^53, strings, a date key, NULLs, a tie,
a measure filter, a derived metric and no rows; MySQL and Dremio refuse the aggregation.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from ._seed import SCHEMA as SEED_SCHEMA
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

TABLE = "first_last_values"

#: The other tables of the multi-fact query: the groups every fact joins to,
#: and two more facts, so the measures' column has three UNION legs.
GROUPS, OTHER, MORE = "first_last_groups", "first_last_other", "first_last_more"
GROUP_ROWS = [("A",), ("B",), ("C",), ("D",), ("E",)]
OTHER_ROWS = [("A", 1), ("B", 2)]
MORE_ROWS = [("A", 10), ("D", 20)]

SCHEMAS = {
    "bigquery": SEED_SCHEMA,
    "snowflake": SEED_SCHEMA,
    "databricks": SEED_SCHEMA,
    "dremio": "$scratch",
}

#: (string, bigint, date) as each engine spells them in a CAST.
TYPES: dict[str, tuple[str, str, str]] = {
    "duckdb": ("VARCHAR", "BIGINT", "DATE"),
    "postgres": ("TEXT", "BIGINT", "DATE"),
    "mysql": ("CHAR(24)", "SIGNED", "DATE"),
    "clickhouse": ("Nullable(String)", "Nullable(Int64)", "Nullable(Date)"),
    "snowflake": ("VARCHAR", "BIGINT", "DATE"),
    "bigquery": ("STRING", "INT64", "DATE"),
    "databricks": ("STRING", "BIGINT", "DATE"),
    "dremio": ("VARCHAR", "BIGINT", "DATE"),
}

#: (group, sequence key, value, label, day). The label is the value spelled
#: out, so first / last of it are the same rows'.
ROWS: list[tuple[str, int | None, int | None, str | None, str | None]] = [
    # The last key, 3, is tied: the greater value, 7, for last.
    ("A", 1, 10, "ten", "2026-01-01"),
    ("A", 3, 5, "five", "2026-03-01"),
    ("A", 3, 7, "seven", "2026-03-01"),
    ("A", 2, 100, "hundred", "2026-02-01"),
    # A NULL value and a NULL key are both skipped.
    ("B", 1, None, None, "2026-01-01"),
    ("B", 2, 4, "four", "2026-02-01"),
    ("B", None, 99, "ninety-nine", None),
    # No row with both.
    ("C", 1, None, None, "2026-01-01"),
    # Past a double's 2^53.
    ("D", 5, 9007199254740993, "big", "2026-05-01"),
    ("D", 4, 1, "one", "2026-04-01"),
    # A group whose key is NULL, as any unmatched join makes one: Dremio's
    # ordered ARRAY_AGG returned an empty array for it.
    (None, 2, 3, "three", "2026-02-01"),
]

MODEL_YAML = """
version: "1.0"
name: first_last_vendor
dataObjects:
  Groups:
    code: {groups}
    schema: '{schema}'
    columns:
      Group: {{code: grp, abstractType: string}}
  Values:
    code: {table}
    schema: '{schema}'
    columns:
      Value Group: {{code: grp, abstractType: string}}
      Seq: {{code: seq, abstractType: int}}
      Value: {{code: val, abstractType: int}}
      Label: {{code: label, abstractType: string}}
      Day: {{code: day, abstractType: date}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Value Group], columnsTo: [Group]}}
  Other:
    code: {other}
    schema: '{schema}'
    columns:
      Other Group: {{code: grp, abstractType: string}}
      Amount: {{code: amt, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Other Group], columnsTo: [Group]}}
  More:
    code: {more}
    schema: '{schema}'
    columns:
      More Group: {{code: grp, abstractType: string}}
      Amount: {{code: amt, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [More Group], columnsTo: [Group]}}
dimensions:
  Group: {{dataObject: Groups, column: Group, resultType: string}}
measures:
  Other Total:
    columns: [{{dataObject: Other, column: Amount}}]
    aggregation: sum
  More Total:
    columns: [{{dataObject: More, column: Amount}}]
    aggregation: sum
  Last Day:
    columns: [{{dataObject: Values, column: Day}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Values, column: Seq}}}}
  Last Value:
    columns: [{{dataObject: Values, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Values, column: Seq}}}}
  First Value:
    columns: [{{dataObject: Values, column: Value}}]
    aggregation: first
    withinGroup: {{column: {{dataObject: Values, column: Seq}}}}
  Last Label:
    columns: [{{dataObject: Values, column: Label}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Values, column: Seq}}}}
  Latest Value:
    columns: [{{dataObject: Values, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Values, column: Day}}}}
  Last Small Value:
    columns: [{{dataObject: Values, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Values, column: Seq}}}}
    filters:
      - column: {{dataObject: Values, column: Value}}
        operator: lt
        values: [{{dataType: int, valueInt: 50}}]
metrics:
  Last Doubled:
    expression: '{{[Last Value]}} * 2'
"""


def _literal(value: str | int | None, type_name: str) -> str:
    text = "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)
    return f"CAST({text} AS {type_name})"


def _prepare(target: VendorTarget) -> SemanticModel:
    dialect = DialectRegistry.get(target.dialect)
    schema = SCHEMAS.get(target.dialect)
    ref = dialect.quote_identifier(TABLE)
    if schema:
        ref = f"{dialect.quote_identifier(schema)}.{ref}"
    text, integer, day = TYPES[target.dialect]
    columns = (
        ("grp", text),
        ("seq", integer),
        ("val", integer),
        ("label", text),
        ("day", day),
    )
    legs = " UNION ALL ".join(
        "SELECT "
        + ", ".join(
            f"{_literal(value, type_name)} AS {dialect.quote_identifier(name)}"
            for value, (name, type_name) in zip(row, columns, strict=True)
        )
        for row in ROWS
    )
    _create(target, ref, legs)
    for name, rows, types in (
        (GROUPS, GROUP_ROWS, (("grp", text),)),
        (OTHER, OTHER_ROWS, (("grp", text), ("amt", integer))),
        (MORE, MORE_ROWS, (("grp", text), ("amt", integer))),
    ):
        table = dialect.quote_identifier(name)
        if schema:
            table = f"{dialect.quote_identifier(schema)}.{table}"
        select = " UNION ALL ".join(
            "SELECT "
            + ", ".join(
                f"{_literal(value, type_name)} AS {dialect.quote_identifier(column)}"
                for value, (column, type_name) in zip(row, types, strict=True)
            )
            for row in rows
        )
        _create(target, table, select)
    yaml_text = MODEL_YAML.format(
        table=TABLE, groups=GROUPS, other=OTHER, more=MORE, schema=schema or ""
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _create(target: VendorTarget, ref: str, select: str) -> None:
    engine = " ENGINE = Memory" if target.dialect == "clickhouse" else ""
    for statement in (f"DROP TABLE IF EXISTS {ref}", f"CREATE TABLE {ref}{engine} AS {select}"):
        # DDL returns no cursor description for the fixture to read.
        with contextlib.suppress(TypeError):
            target.execute(statement)


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


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    names = ["Last Value", "First Value", "Last Label", "Latest Value", "Last Small Value"]
    rows = _run(target, model, ["Group"], names)
    got = {r["group"]: tuple(r[n.lower()] for n in names) for r in rows}
    want = {
        "A": (7, 10, "seven", 7, 7),
        "B": (4, 4, "four", 4, 4),
        "C": (None, None, None, None, None),
        "D": (9007199254740993, 1, "big", 9007199254740993, 1),
        None: (3, 3, "three", 3, 3),
    }
    assert got == want, f"{target.name}: {got}"

    # Beside two other facts, the measures' value and sort-key columns have three
    # UNION legs, the first two padding them: text and dates keep their type
    # rather than the numeric default, and the pads are typed as the key.
    multi = ["Other Total", "More Total", "Last Label", "Last Day", "Last Value"]
    rows = _run(target, model, ["Group"], multi)
    got_multi = {
        r["group"]: tuple(
            str(r[m.lower()])[:10] if m == "Last Day" and r[m.lower()] else r[m.lower()]
            for m in multi
        )
        for r in rows
    }
    want_multi = {
        "A": (1, 10, "seven", "2026-03-01", 7),
        "B": (2, None, "four", "2026-02-01", 4),
        # C's one row has a day but no value.
        "C": (None, None, None, "2026-01-01", None),
        "D": (None, 20, "big", "2026-05-01", 9007199254740993),
        None: (None, None, "three", "2026-02-01", 3),
    }
    assert got_multi == want_multi, f"{target.name}: {got_multi}"

    # Inside a formula the value is one operand.
    rows = _run(target, model, ["Group"], ["Last Doubled"], [_group("A")])
    assert [r["last doubled"] for r in rows] == [14], f"{target.name}: {rows}"

    # Ungrouped, over every group.
    rows = _run(target, model, [], ["Last Value", "First Value", "Latest Value"])
    got_all = [(r["last value"], r["first value"], r["latest value"]) for r in rows]
    assert got_all == [(9007199254740993, 10, 9007199254740993)], f"{target.name}: {got_all}"

    # No rows at all: NULL.
    rows = _run(target, model, [], ["Last Value"], [_group("none")])
    assert [r["last value"] for r in rows] == [None], f"{target.name}: {rows}"


#: Keys whose type the multi-fact legs' pads must match: an exact decimal past
#: a double's 2^53, and an instant under a query timezone, around the hour
#: Berlin falls back (00:30Z is 02:30 there, 01:10Z is 02:10: by wall clock the
#: earlier trade would read as the last).
KEYS = "first_last_keys"
#: Also keys a guessed pad type rounds: milliseconds (cast to seconds on
#: ClickHouse they tied) and a double near zero (cast to a decimal, 0).
KEY_ROWS = [
    ("E", "9007199254740992", "2026-10-25 01:10:00", "2026-01-01 12:00:00.100", "1e-25", 9),
    ("E", "9007199254740993", "2026-10-25 00:30:00", "2026-01-01 12:00:00.200", "2e-25", 1),
]
#: (decimal(20, 0), timestamp with zone, timestamp with milliseconds, double)
#: as each engine spells them in a CAST.
KEY_TYPES: dict[str, tuple[str, str, str, str]] = {
    "duckdb": ("DECIMAL(20, 0)", "TIMESTAMPTZ", "TIMESTAMP", "DOUBLE"),
    "postgres": ("NUMERIC(20, 0)", "TIMESTAMPTZ", "TIMESTAMP(3)", "DOUBLE PRECISION"),
    "clickhouse": (
        "Nullable(Decimal(20, 0))",
        "Nullable(DateTime64(3, 'UTC'))",
        "Nullable(DateTime64(3, 'UTC'))",
        "Nullable(Float64)",
    ),
    "snowflake": ("NUMBER(20, 0)", "TIMESTAMP_TZ", "TIMESTAMP_NTZ(3)", "DOUBLE"),
    "bigquery": ("NUMERIC", "TIMESTAMP", "DATETIME", "FLOAT64"),
    "databricks": ("DECIMAL(20, 0)", "TIMESTAMP", "TIMESTAMP_NTZ", "DOUBLE"),
}

KEYS_YAML = """
version: "1.0"
name: first_last_keys
settings:
  queryTimezone: Europe/Berlin
dataObjects:
  Groups:
    code: {groups}
    schema: '{schema}'
    columns:
      Group: {{code: grp, abstractType: string}}
  Keys:
    code: {keys}
    schema: '{schema}'
    columns:
      Key Group: {{code: grp, abstractType: string}}
      Exact Key: {{code: dkey, abstractType: float}}
      Instant: {{code: tkey, abstractType: timestamp_tz}}
      Moment: {{code: mkey, abstractType: timestamp}}
      Tiny Key: {{code: fkey, abstractType: float}}
      Value: {{code: val, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Key Group], columnsTo: [Group]}}
  Other:
    code: {other}
    schema: '{schema}'
    columns:
      Other Group: {{code: grp, abstractType: string}}
      Amount: {{code: amt, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Other Group], columnsTo: [Group]}}
dimensions:
  Group: {{dataObject: Groups, column: Group, resultType: string}}
measures:
  Other Total:
    columns: [{{dataObject: Other, column: Amount}}]
    aggregation: sum
  Last By Exact Key:
    columns: [{{dataObject: Keys, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Keys, column: Exact Key}}}}
  Last By Instant:
    columns: [{{dataObject: Keys, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Keys, column: Instant}}}}
  Last By Moment:
    columns: [{{dataObject: Keys, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Keys, column: Moment}}}}
  Last By Tiny Key:
    columns: [{{dataObject: Keys, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Keys, column: Tiny Key}}}}
"""


def _assert_key_types(target: VendorTarget) -> None:
    """Alone and beside another fact: the pads keep the key's type."""
    _prepare(target)
    dialect = DialectRegistry.get(target.dialect)
    schema = SCHEMAS.get(target.dialect)
    text, integer, _day = TYPES[target.dialect]
    exact, instant, moment, double = KEY_TYPES[target.dialect]
    columns = (
        ("grp", text),
        ("dkey", exact),
        ("tkey", instant),
        ("mkey", moment),
        ("fkey", double),
        ("val", integer),
    )
    table = dialect.quote_identifier(KEYS)
    if schema:
        table = f"{dialect.quote_identifier(schema)}.{table}"
    select = " UNION ALL ".join(
        "SELECT "
        + ", ".join(
            f"{_literal(value, type_name)} AS {dialect.quote_identifier(name)}"
            for value, (name, type_name) in zip(row, columns, strict=True)
        )
        for row in KEY_ROWS
    )
    _create(target, table, select)
    yaml_text = KEYS_YAML.format(groups=GROUPS, keys=KEYS, other=OTHER, schema=schema or "")
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    keyed = ["Last By Exact Key", "Last By Instant", "Last By Moment", "Last By Tiny Key"]
    for extra in ([], ["Other Total"]):
        rows = _run(target, model, ["Group"], [*extra, *keyed], [_group("E")])
        got = [tuple(r[m.lower()] for m in keyed) for r in rows]
        assert got == [(1, 9, 1, 1)], f"{target.name} {extra}: {got}"


def _group(value: str) -> QueryFilter:
    return QueryFilter(field="Group", op="=", value=value)


def test_duckdb_first_last(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)
    _assert_key_types(vendor_duckdb)


def test_postgres_first_last(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)
    _assert_key_types(vendor_postgres)


def test_mysql_refuses_first_last(vendor_mysql: VendorTarget) -> None:
    model = _prepare(vendor_mysql)
    with pytest.raises(UnsupportedAggregationError, match="LAST"):
        _run(vendor_mysql, model, ["Group"], ["Last Value"])


def test_dremio_refuses_first_last(vendor_dremio: VendorTarget) -> None:
    """Its ordered ARRAY_AGG answers an empty array for a NULL group key."""
    model = _prepare(vendor_dremio)
    with pytest.raises(UnsupportedAggregationError, match="LAST"):
        _run(vendor_dremio, model, ["Group"], ["Last Value"])


def test_clickhouse_first_last(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)
    _assert_key_types(vendor_clickhouse)


def test_snowflake_first_last(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)
    _assert_key_types(vendor_snowflake)


def test_bigquery_first_last(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)
    _assert_key_types(vendor_bigquery)


def test_databricks_first_last(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)
    _assert_key_types(vendor_databricks)
