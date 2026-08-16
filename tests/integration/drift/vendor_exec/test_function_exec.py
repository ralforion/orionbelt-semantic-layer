"""Execution matrix for the portable function catalog.

Every canonical call in ``models/functions.py`` is rendered for the vendor's
dialect, executed there, and compared to the value the catalog documents. This
is the part that catches the divergences a compile-only golden cannot: an
engine whose ``concat`` drops a NULL, whose ``length`` counts bytes, or whose
``split_part`` hands back the last field instead of an empty string. Those
return wrong *numbers and strings*, not errors.

DuckDB is the oracle — the canonical form runs there unchanged — so its column
doubles as a check that the catalog's documented values are the ones DuckDB
actually produces.

Dremio has no fixture here and is the one dialect the matrix does not cover;
its renderings are derived from Dremio's published function reference.

Gated by the ``docker`` pytest marker::

    uv run pytest -m docker tests/integration/drift/vendor_exec/test_function_exec.py
"""

from __future__ import annotations

from typing import Any

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.functions import FUNCTION_CATALOG, FunctionSpec

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

CATALOG = list(FUNCTION_CATALOG.values())


def _matches(expected: str | int | float | bool | None, actual: Any) -> bool:
    """Whether *actual* is the documented value, across driver type mappings.

    Booleans come back as ``1``/``0`` from MySQL and ClickHouse, integers as
    ``Decimal`` from several drivers, and strings are strings everywhere — so
    each expected type is compared in its own terms rather than by equality on
    whatever Python object the driver chose.
    """
    if expected is None:
        return actual is None
    if actual is None:
        return False
    if isinstance(expected, bool):
        return bool(actual) is expected
    if isinstance(expected, int):
        return int(actual) == expected
    return str(actual) == str(expected)


def _assert_catalog_values(spec: FunctionSpec, vendor: VendorTarget) -> None:
    """Execute every example of *spec* on *vendor* in one round trip."""
    engine = DialectRegistry.get(vendor.dialect)
    projections = []
    for index, example in enumerate(spec.examples):
        ast = parse_expression(tokenize_metric_formula(example.call))
        projections.append(f"{engine.compile_expr(ast)} AS c{index}")
    sql = "SELECT " + ", ".join(projections)

    row = vendor.execute(sql)[0]
    values = list(row.values())
    mismatches = [
        f"{example.call} -> {values[index]!r}, catalog says {example.expect!r}"
        for index, example in enumerate(spec.examples)
        if not _matches(example.expect, values[index])
    ]
    assert not mismatches, (
        f"{vendor.name} disagrees with the catalog on '{spec.name}':\n  "
        + "\n  ".join(mismatches)
        + f"\nSQL: {sql}"
    )


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_duckdb_function_exec(spec: FunctionSpec, vendor_duckdb: VendorTarget) -> None:
    """DuckDB — the oracle the canonical forms are defined against."""
    _assert_catalog_values(spec, vendor_duckdb)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_postgres_function_exec(spec: FunctionSpec, vendor_postgres: VendorTarget) -> None:
    """Postgres 16 testcontainer."""
    _assert_catalog_values(spec, vendor_postgres)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_mysql_function_exec(spec: FunctionSpec, vendor_mysql: VendorTarget) -> None:
    """MySQL 8 testcontainer."""
    _assert_catalog_values(spec, vendor_mysql)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_clickhouse_function_exec(spec: FunctionSpec, vendor_clickhouse: VendorTarget) -> None:
    """ClickHouse testcontainer."""
    _assert_catalog_values(spec, vendor_clickhouse)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_snowflake_function_exec(spec: FunctionSpec, vendor_snowflake: VendorTarget) -> None:
    """Live Snowflake account."""
    _assert_catalog_values(spec, vendor_snowflake)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_bigquery_function_exec(spec: FunctionSpec, vendor_bigquery: VendorTarget) -> None:
    """Live BigQuery project."""
    _assert_catalog_values(spec, vendor_bigquery)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_databricks_function_exec(spec: FunctionSpec, vendor_databricks: VendorTarget) -> None:
    """Live Databricks SQL warehouse."""
    _assert_catalog_values(spec, vendor_databricks)
