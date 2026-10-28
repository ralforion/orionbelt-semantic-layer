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
      Amount: {code: amount, abstractType: decimal, numClass: additive}
measures:
  Qty Avg:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: avg
  Amount Avg:
    columns: [{dataObject: Charges, column: Amount}]
    resultType: decimal
    aggregation: avg
"""

# value list -> exact average. Every one of these has caught something.
CASES: list[tuple[str, list[int | None], str | None]] = [
    ("large", [1000000000000000002, 1000000000000000004], "1000000000000000003"),
    ("bigsum", [9000000000000000000, 9000000000000000000], "9000000000000000000"),
    ("negatives", [-3, -2], "-2.5"),
    ("mixedsign", [-3, 2], "-0.5"),
    ("thirds", [1, 1, 2], "1.33"),
    # Rounds up: ClickHouse's divideDecimal truncated it to 1.66.
    ("twothirds", [1, 2, 2], "1.67"),
    ("single", [7], "7"),
    ("small", [10, 21], "15.5"),
    ("allnull", [None, None], None),
]

# A decimal column's average: a double on DuckDB, ClickHouse and Dremio until
# each got an exact route. Each value below came back wrong on at least one.
DECIMAL_CASES: list[tuple[str, list[str | None], str | None]] = [
    # Past a double's mantissa: .565 rounds to .57; a double gave .56.
    ("tie", ["12345678901234.56", "12345678901234.57"], "12345678901234.57"),
    # At the type's width: a double gave 1E+16, which DuckDB's cast refused
    # and Dremio's turned into 0.
    ("wide", ["9999999999999999.99", "9999999999999999.99"], "9999999999999999.99"),
    ("twothirds", ["1.00", "2.00", "2.00"], "1.67"),
    ("negativetie", ["-2.36", "-2.37"], "-2.37"),
    ("single", ["7.25"], "7.25"),
    ("allnull", [None, None], None),
]


def _decimal_rows(alias: str, column: str, values: list[str | None], dialect: str) -> str:
    """A FROM-able source of one DECIMAL(18, 2) column holding *values*."""
    type_name = {
        "clickhouse": "Nullable(Decimal(18, 2))",
        # BigQuery takes no parameters in a CAST.
        "bigquery": "NUMERIC",
    }.get(dialect, "DECIMAL(18, 2)")
    legs = " UNION ALL ".join(
        f"SELECT CAST({'NULL' if v is None else repr(v)} AS {type_name}) AS {column}"
        for v in values
    )
    return f"({legs}) {alias}"


def _projection(dialect: str, measure: str = "Qty Avg") -> str:
    """The measure expression OBSL emits, lifted out of its SELECT."""
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    sql = (
        CompilationPipeline()
        .compile(QueryObject(select=QuerySelect(dimensions=[], measures=[measure])), model, dialect)
        .sql
    )
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    return match.group(1).strip()


def _assert_exact_average(target: VendorTarget) -> None:
    dia = DialectRegistry.get(target.dialect)
    alias = dia.quote_identifier("Charges")
    qty = dia.quote_identifier("qty")
    amount = dia.quote_identifier("amount")
    sources = [
        (label, _projection(target.dialect), target.rows_of(alias, qty, values), want)
        for label, values, want in CASES
    ] + [
        (
            f"decimal {label}",
            _projection(target.dialect, "Amount Avg"),
            _decimal_rows(alias, amount, values, target.dialect),
            want,
        )
        for label, values, want in DECIMAL_CASES
    ]

    mismatches = []
    for label, projection, source, want in sources:
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


def test_dremio_exact_avg(vendor_dremio: VendorTarget) -> None:
    _assert_exact_average(vendor_dremio)
