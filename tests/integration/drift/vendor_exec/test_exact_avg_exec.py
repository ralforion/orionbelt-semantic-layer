"""Execute the exact-AVG rewrite, per vendor, against values the seed lacks.

``AVG`` is a floating-point aggregate on several engines whatever the input
type, so it drifts once the average passes a ``double`` mantissa. OBSL rewrites
it into an exact form where the engine offers one (#318, #326).

Nothing executed that rewrite until this module. ``test_exact_avg.py`` asserts
the emitted **SQL**, and the vendor measure sweep covers only the container
engines, so the cloud dialects had no aggregate coverage at all. That is how
Databricks sat in the wrong group for two months while every test passed
(#322): the unit test checks a dialect is *classified*, not that the
classification was *measured*.

The corpus seed cannot serve this - its values are far below the boundary, and
adding large ones would change the DuckDB golden for every vendor. So the rows
come from ``VendorTarget.rows_of``, which each fixture spells for its own
engine (#330).

The cases are the ones that have actually caught defects: a sum past 64 bits
found the accumulator wrapping on Dremio and ClickHouse and raising on
Databricks, and an empty group found ClickHouse ``divideDecimal`` raising where
``AVG`` returns NULL.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: exact_avg_exec
dataObjects:
  Charges:
    code: charges
    columns:
      Qty: {code: qty, abstractType: int}
measures:
  Qty Avg:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: avg
"""

# value list -> exact average. Every one of these has caught something.
CASES: list[tuple[str, list[int | None], str | None]] = [
    ("large", [1000000000000000002, 1000000000000000004], "1000000000000000003"),
    ("bigsum", [9000000000000000000, 9000000000000000000], "9000000000000000000"),
    ("negatives", [-3, -2], "-2.5"),
    ("mixedsign", [-3, 2], "-0.5"),
    ("thirds", [1, 1, 2], "1.33"),
    ("single", [7], "7"),
    ("small", [10, 21], "15.5"),
    ("allnull", [None, None], None),
]

# DuckDB has no exact division at all, so it keeps the plain AVG and a large
# average overflows the default type rather than returning a wrong number
# (#316). Executing the same cases there would assert that limitation, which
# ``test_data_types.py`` already pins closer to the cause.
NO_EXACT_ROUTE = {"duckdb"}


def _projection(dialect: str) -> str:
    """The measure expression OBSL emits, lifted out of its SELECT."""
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    sql = (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Qty Avg"])), model, dialect
        )
        .sql
    )
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    return match.group(1).strip()


def _assert_exact_average(target: VendorTarget) -> None:
    if target.dialect in NO_EXACT_ROUTE:
        pytest.skip(f"{target.dialect} has no exact division; see #316")
    dia = DialectRegistry.get(target.dialect)
    alias = dia.quote_identifier("Charges")
    column = dia.quote_identifier("qty")
    projection = _projection(target.dialect)

    mismatches = []
    for label, values, want in CASES:
        source = target.rows_of(alias, column, values)
        rows = target.execute(f"SELECT {projection} FROM {source}")
        got = next(iter(rows[0].values()))
        if want is None:
            if got is not None:
                mismatches.append(f"{label}: {got!r}, expected NULL")
        elif got is None or Decimal(str(got)) != Decimal(want):
            mismatches.append(f"{label}: {got!r}, expected {want}")

    assert not mismatches, f"{target.name} does not average exactly:\n  " + "\n  ".join(mismatches)


def test_duckdb_exact_avg(vendor_duckdb: VendorTarget) -> None:
    _assert_exact_average(vendor_duckdb)


def test_postgres_exact_avg(vendor_postgres: VendorTarget) -> None:
    _assert_exact_average(vendor_postgres)


def test_mysql_exact_avg(vendor_mysql: VendorTarget) -> None:
    _assert_exact_average(vendor_mysql)


def test_clickhouse_exact_avg(vendor_clickhouse: VendorTarget) -> None:
    _assert_exact_average(vendor_clickhouse)


def test_snowflake_exact_avg(vendor_snowflake: VendorTarget) -> None:
    _assert_exact_average(vendor_snowflake)


def test_bigquery_exact_avg(vendor_bigquery: VendorTarget) -> None:
    _assert_exact_average(vendor_bigquery)


def test_databricks_exact_avg(vendor_databricks: VendorTarget) -> None:
    _assert_exact_average(vendor_databricks)
