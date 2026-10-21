"""A sum of an ``int`` column without ``resultType`` returns its exact total.

The omitted ``resultType`` used to default to ``float``, so the sum was cast to
the ``decimal(18, 2)`` default and a total past sixteen digits raised (or, on
MySQL, saturated) although the column held every row legally. Inferred from the
column it is ``int``, and the sum is the 64-bit sum every engine computes.
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
name: inferred_result_type_exec
dataObjects:
  Charges:
    code: charges
    columns:
      Qty: {code: qty, abstractType: int}
measures:
  Units:
    columns: [{dataObject: Charges, column: Qty}]
    aggregation: sum
"""

ROWS = [100000000000000000, 3]
TOTAL = Decimal("100000000000000003")


def _assert_exact_total(target: VendorTarget) -> None:
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(dimensions=[], measures=["Units"]))
    sql = CompilationPipeline().compile(query, model, target.dialect).sql
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    dia = DialectRegistry.get(target.dialect)
    source = target.rows_of(dia.quote_identifier("Charges"), dia.quote_identifier("qty"), ROWS)
    rows = target.execute(f"SELECT {match.group(1).strip()} FROM {source}")
    got = next(iter(rows[0].values()))
    assert got is not None and Decimal(str(got)) == TOTAL, f"{target.name}: {got}"


def test_duckdb_inferred_integer_sum(vendor_duckdb: VendorTarget) -> None:
    _assert_exact_total(vendor_duckdb)


def test_postgres_inferred_integer_sum(vendor_postgres: VendorTarget) -> None:
    _assert_exact_total(vendor_postgres)


def test_mysql_inferred_integer_sum(vendor_mysql: VendorTarget) -> None:
    _assert_exact_total(vendor_mysql)


def test_clickhouse_inferred_integer_sum(vendor_clickhouse: VendorTarget) -> None:
    _assert_exact_total(vendor_clickhouse)


def test_bigquery_inferred_integer_sum(vendor_bigquery: VendorTarget) -> None:
    _assert_exact_total(vendor_bigquery)


def test_snowflake_inferred_integer_sum(vendor_snowflake: VendorTarget) -> None:
    _assert_exact_total(vendor_snowflake)


def test_databricks_inferred_integer_sum(vendor_databricks: VendorTarget) -> None:
    _assert_exact_total(vendor_databricks)


def test_dremio_inferred_integer_sum(vendor_dremio: VendorTarget) -> None:
    _assert_exact_total(vendor_dremio)
