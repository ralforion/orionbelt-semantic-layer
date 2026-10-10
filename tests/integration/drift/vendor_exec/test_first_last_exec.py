"""``aggregation: first`` / ``last`` compiled and run on each engine.

The value at the least / greatest ``withinGroup`` key of each group. The
vendors' own ``first`` / ``last`` (DuckDB, Databricks, ClickHouse's ``any``)
take the first row met, and their ordered ``MAX_BY`` / ``arg_max`` break a tie
on the key arbitrarily, so each rendering orders by the (key, value) pair: a
tie goes to the greatest value for ``last`` and the least for ``first``. Rows
without a value or a key are skipped. The same answers are asserted on seven
engines, for integers past a double's 2^53, strings, a date key, NULLs, a tie,
a measure filter, a derived metric and no rows; MySQL refuses the aggregation.
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
]

MODEL_YAML = """
version: "1.0"
name: first_last_vendor
dataObjects:
  Values:
    code: {table}
    schema: '{schema}'
    columns:
      Group: {{code: grp, abstractType: string}}
      Seq: {{code: seq, abstractType: int}}
      Value: {{code: val, abstractType: int}}
      Label: {{code: label, abstractType: string}}
      Day: {{code: day, abstractType: date}}
dimensions:
  Group: {{dataObject: Values, column: Group, resultType: string}}
measures:
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
    }
    assert got == want, f"{target.name}: {got}"

    # Inside a formula the value is one operand.
    rows = _run(target, model, ["Group"], ["Last Doubled"], [_group("A")])
    assert [r["last doubled"] for r in rows] == [14], f"{target.name}: {rows}"

    # Ungrouped, over every group.
    rows = _run(target, model, [], ["Last Value", "First Value", "Latest Value"])
    got_all = [(r["last value"], r["first value"], r["latest value"]) for r in rows]
    assert got_all == [(9007199254740993, 10, 9007199254740993)], f"{target.name}: {got_all}"

    # No rows at all: NULL. Dremio returns no row for an ungrouped ordered
    # ARRAY_AGG over no rows, as for its median.
    rows = _run(target, model, [], ["Last Value"], [_group("none")])
    want_none = [] if target.dialect == "dremio" else [None]
    assert [r["last value"] for r in rows] == want_none, f"{target.name}: {rows}"


def _group(value: str) -> QueryFilter:
    return QueryFilter(field="Group", op="=", value=value)


def test_duckdb_first_last(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_first_last(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_refuses_first_last(vendor_mysql: VendorTarget) -> None:
    model = _prepare(vendor_mysql)
    with pytest.raises(UnsupportedAggregationError, match="LAST"):
        _run(vendor_mysql, model, ["Group"], ["Last Value"])


def test_clickhouse_first_last(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_snowflake_first_last(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_bigquery_first_last(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_databricks_first_last(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_first_last(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
