"""A ``resultType: float`` sum or average returns the engine's double, unrounded.

It was cast to the ``decimal(18, 2)`` default, so the average of 1, 1 and 2
came back 1.33. Cast to each engine's double it is 1.3333333333333333. The
cast around the aggregate is not enough on its own: ClickHouse and Dremio
accumulate 64-bit integers in 64 bits and wrap, and a DISTINCT average must
deduplicate before anything rounds two integers into one double.
"""

from __future__ import annotations

import re

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: float_measure_double_exec
dataObjects:
  Charges:
    code: charges
    columns:
      Qty: {code: qty, abstractType: int}
measures:
  Mean Units:
    columns: [{dataObject: Charges, column: Qty}]
    aggregation: avg
    resultType: float
  Total Units:
    columns: [{dataObject: Charges, column: Qty}]
    aggregation: sum
    resultType: float
  Distinct Total Units:
    columns: [{dataObject: Charges, column: Qty}]
    aggregation: sum
    distinct: true
    resultType: float
  Distinct Mean Units:
    columns: [{dataObject: Charges, column: Qty}]
    aggregation: avg
    distinct: true
    resultType: float
"""

# (measure, rows, expected, tolerance)
CASES = {
    "mean": ("Mean Units", [1, 1, 2], 4 / 3, 1e-12),
    # Wrapped to -446744073709551616 on ClickHouse and Dremio.
    "sum_past_64_bits": ("Total Units", [9 * 10**18, 9 * 10**18], 1.8e19, 1e4),
    # Wrapped to -1.4467440737095516e18 on ClickHouse.
    "distinct_sum_past_64_bits": (
        "Distinct Total Units",
        [9 * 10**18, 8 * 10**18, 8 * 10**18],
        1.7e19,
        1e4,
    ),
    "distinct_mean_past_64_bits": (
        "Distinct Mean Units",
        [9 * 10**18, 8 * 10**18, 8 * 10**18],
        8.5e18,
        1e4,
    ),
    # Averaged two values, 4503599627370496, once 2**53 and 2**53 + 1 became
    # one double before DISTINCT saw them. Doubles are 1 apart at this size.
    "distinct_mean": (
        "Distinct Mean Units",
        [0, 2**53, 2**53 + 1, 2**53 + 1],
        (2 * 2**53 + 1) / 3,
        2.0,
    ),
}


# These two refuse an integer sum their 64-bit accumulator cannot hold, an
# error rather than a wrong total, and keep doing so for a float measure.
RAISES_ON_OVERFLOW = {"bigquery", "databricks"}


def _assert_case(target: VendorTarget, case: str) -> None:
    measure, rows, expected, tolerance = CASES[case]
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(dimensions=[], measures=[measure]))
    sql = CompilationPipeline().compile(query, model, target.dialect).sql
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    dia = DialectRegistry.get(target.dialect)
    source = target.rows_of(dia.quote_identifier("Charges"), dia.quote_identifier("qty"), rows)
    statement = f"SELECT {match.group(1).strip()} FROM {source}"
    if case.endswith("sum_past_64_bits") and target.name in RAISES_ON_OVERFLOW:
        with pytest.raises(Exception, match="(?i)overflow"):
            target.execute(statement)
        return
    got = next(iter(target.execute(statement)[0].values()))
    assert got is not None and abs(float(got) - expected) <= tolerance, f"{target.name}: {got}"


@pytest.mark.parametrize("case", sorted(CASES))
def test_duckdb_float_measure(vendor_duckdb: VendorTarget, case: str) -> None:
    _assert_case(vendor_duckdb, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_postgres_float_measure(vendor_postgres: VendorTarget, case: str) -> None:
    _assert_case(vendor_postgres, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_mysql_float_measure(vendor_mysql: VendorTarget, case: str) -> None:
    _assert_case(vendor_mysql, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_clickhouse_float_measure(vendor_clickhouse: VendorTarget, case: str) -> None:
    _assert_case(vendor_clickhouse, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_bigquery_float_measure(vendor_bigquery: VendorTarget, case: str) -> None:
    _assert_case(vendor_bigquery, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_snowflake_float_measure(vendor_snowflake: VendorTarget, case: str) -> None:
    _assert_case(vendor_snowflake, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_databricks_float_measure(vendor_databricks: VendorTarget, case: str) -> None:
    _assert_case(vendor_databricks, case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_dremio_float_measure(vendor_dremio: VendorTarget, case: str) -> None:
    _assert_case(vendor_dremio, case)
