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
``listagg`` over that group is the same cut, fixed by the same hint.
"""

from __future__ import annotations

import contextlib
import statistics
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
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

#: (string, bigint, decimal(10, 2)) as each engine spells them in a CAST.
TYPES: dict[str, tuple[str, str, str]] = {
    "duckdb": ("VARCHAR", "BIGINT", "DECIMAL(10, 2)"),
    "postgres": ("TEXT", "BIGINT", "NUMERIC(10, 2)"),
    "mysql": ("CHAR(8)", "SIGNED", "DECIMAL(10, 2)"),
    "clickhouse": ("Nullable(String)", "Nullable(Int64)", "Nullable(Decimal(10, 2))"),
    "snowflake": ("VARCHAR", "BIGINT", "NUMBER(10, 2)"),
    "bigquery": ("STRING", "INT64", "NUMERIC"),
    "databricks": ("STRING", "BIGINT", "DECIMAL(10, 2)"),
    "dremio": ("VARCHAR", "BIGINT", "DECIMAL(10, 2)"),
}

#: (group, integer, decimal, label)
ROWS: list[tuple[str, int | None, str | None, str | None]] = [
    ("A", 1, "1.25", None),
    ("A", 2, "2.75", None),
    ("A", 10, "10.10", None),
    ("A", 20, "20.20", None),
    ("A", None, None, None),
    ("B", 1, "1.00", None),
    ("B", 2, "2.00", None),
    ("B", 10, "3.50", None),
    ("C", None, None, None),
    *(("L", n, None, f"lbl{n:05d}") for n in range(1, 201)),
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
dimensions:
  Group: {{dataObject: Values, column: Group, resultType: string}}
measures:
  Integer Median:
    columns: [{{dataObject: Values, column: Integer}}]
    aggregation: median
  Decimal Median:
    columns: [{{dataObject: Values, column: Decimal}}]
    aggregation: median
  Integer Total:
    columns: [{{dataObject: Values, column: Integer}}]
    aggregation: sum
    total: true
  Labels:
    columns: [{{dataObject: Values, column: Label}}]
    aggregation: listagg
    delimiter: ","
"""


def _literal(value: str | int | None, type_name: str) -> str:
    text = "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)
    return f"CAST({text} AS {type_name})"


def _prepare(target: VendorTarget) -> SemanticModel:
    dialect = DialectRegistry.get(target.dialect)
    text, integer, decimal = TYPES[target.dialect]
    schema = SCHEMAS.get(target.dialect)
    ref = dialect.quote_identifier(TABLE)
    if schema:
        ref = f"{dialect.quote_identifier(schema)}.{ref}"
    columns = (("grp", text), ("int_val", integer), ("dec_val", decimal), ("label", text))
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
    target: VendorTarget, model: SemanticModel, dimensions: list[str], measures: list[str]
) -> list[dict[str, Any]]:
    query = QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures))
    rows = target.execute(CompilationPipeline().compile(query, model, target.dialect).sql)
    return [{str(k).lower(): v for k, v in row.items()} for row in rows]


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _median(column: int, group: str | None = None) -> Decimal | None:
    values = [
        Decimal(str(row[column]))
        for row in ROWS
        if row[column] is not None and group in (None, row[0])
    ]
    return statistics.median(values) if values else None


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    groups = ["A", "B", "C", "L"]
    want = {g: (_median(1, g), _median(2, g)) for g in groups}
    # Even, odd, all-NULL, and a group of 200 past GROUP_CONCAT's default cut.
    assert want["A"] == (Decimal(6), Decimal("6.425"))
    assert want["L"][0] == Decimal("100.5")

    measures = ["Integer Median", "Decimal Median"]
    for extra in ([], ["Integer Total"]):
        # Beside a total, the medians are computed in a CTE the window reads.
        rows = _run(target, model, ["Group"], [*measures, *extra])
        got = {r["group"]: tuple(_number(r[m.lower()]) for m in measures) for r in rows}
        assert got == want, f"{target.name} {extra}: {got}"

    rows = _run(target, model, [], measures)
    got_all = [tuple(_number(r[m.lower()]) for m in measures) for r in rows]
    assert got_all == [(_median(1), _median(2))], f"{target.name}: {got_all}"

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
