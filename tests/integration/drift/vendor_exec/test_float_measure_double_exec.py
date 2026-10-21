"""A ``resultType: float`` average returns the engine's double, unrounded.

It was cast to the ``decimal(18, 2)`` default, so the average of 1, 1 and 2
came back 1.33. Cast to each engine's double it is 1.3333333333333333.
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
"""

ROWS = [1, 1, 2]


def _assert_unrounded_mean(target: VendorTarget) -> None:
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(dimensions=[], measures=["Mean Units"]))
    sql = CompilationPipeline().compile(query, model, target.dialect).sql
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    dia = DialectRegistry.get(target.dialect)
    source = target.rows_of(dia.quote_identifier("Charges"), dia.quote_identifier("qty"), ROWS)
    rows = target.execute(f"SELECT {match.group(1).strip()} FROM {source}")
    got = next(iter(rows[0].values()))
    assert got is not None and abs(float(got) - 4 / 3) < 1e-12, f"{target.name}: {got}"


def test_duckdb_float_mean(vendor_duckdb: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_duckdb)


def test_postgres_float_mean(vendor_postgres: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_postgres)


def test_mysql_float_mean(vendor_mysql: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_mysql)


def test_clickhouse_float_mean(vendor_clickhouse: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_clickhouse)


def test_bigquery_float_mean(vendor_bigquery: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_bigquery)


def test_snowflake_float_mean(vendor_snowflake: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_snowflake)


def test_databricks_float_mean(vendor_databricks: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_databricks)


def test_dremio_float_mean(vendor_dremio: VendorTarget) -> None:
    _assert_unrounded_mean(vendor_dremio)
