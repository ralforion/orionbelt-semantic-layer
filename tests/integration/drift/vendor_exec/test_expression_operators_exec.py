"""Execution matrix for the expression grammar's ``%``, ``||``, ``ILIKE`` and
``IS [NOT] DISTINCT FROM``.

Each is parsed by OBML's own expression parser, rendered for the vendor's
dialect and executed there: ``%`` as the catalog's ``mod`` (BigQuery has no
``%``), ``||`` as its ``concat`` (MySQL reads ``||`` as OR), ``ILIKE`` as the
case-insensitive match query filters use, and ``IS [NOT] DISTINCT FROM`` in
comparisons every engine has (MySQL has no such operator, ClickHouse only in a
join). A wrong rendering returns a wrong value here, not an error.

Gated by the ``docker`` pytest marker::

    uv run pytest -m docker tests/integration/drift/vendor_exec/test_expression_operators_exec.py
"""

from __future__ import annotations

from typing import Any

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry

from ._catalog_values import matches as _matches
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

CASES: list[tuple[str, Any]] = [
    ("7 % 3", 1),
    ("-7 % 3", -1),
    ("1 + 7 % 3 * 2", 3),
    ("'a' || 'b' || 'c'", "abc"),
    ("'a' || NULL || 'c'", None),
    ("CASE WHEN 'Done' ILIKE 'do%' THEN 1 ELSE 0 END", 1),
    ("CASE WHEN 'Done' NOT ILIKE 'DO%' THEN 1 ELSE 0 END", 0),
    ("CASE WHEN 'done' ILIKE 'DONE' THEN 1 ELSE 0 END", 1),
    ("CASE WHEN 1 IS DISTINCT FROM 2 THEN 1 ELSE 0 END", 1),
    ("CASE WHEN 1 IS DISTINCT FROM 1 THEN 1 ELSE 0 END", 0),
    ("CASE WHEN 1 IS DISTINCT FROM NULL THEN 1 ELSE 0 END", 1),
    ("CASE WHEN NULL IS DISTINCT FROM NULL THEN 1 ELSE 0 END", 0),
    ("CASE WHEN 1 IS NOT DISTINCT FROM NULL THEN 1 ELSE 0 END", 0),
    ("CASE WHEN NULL IS NOT DISTINCT FROM NULL THEN 1 ELSE 0 END", 1),
]


def _assert_operators(vendor: VendorTarget) -> None:
    """Execute every case on *vendor* in one round trip."""
    engine = DialectRegistry.get(vendor.dialect)
    projections = [
        f"{engine.compile_expr(parse_expression(tokenize_metric_formula(text)))} AS c{index}"
        for index, (text, _) in enumerate(CASES)
    ]
    sql = "SELECT " + ", ".join(projections)
    values = list(vendor.execute(sql)[0].values())
    mismatches = [
        f"{text} -> {values[index]!r}, expected {expected!r}"
        for index, (text, expected) in enumerate(CASES)
        if not _matches(expected, values[index])
    ]
    assert not mismatches, f"{vendor.name}:\n  " + "\n  ".join(mismatches) + f"\nSQL: {sql}"


def test_duckdb_operators(vendor_duckdb: VendorTarget) -> None:
    _assert_operators(vendor_duckdb)


def test_postgres_operators(vendor_postgres: VendorTarget) -> None:
    _assert_operators(vendor_postgres)


def test_mysql_operators(vendor_mysql: VendorTarget) -> None:
    _assert_operators(vendor_mysql)


def test_clickhouse_operators(vendor_clickhouse: VendorTarget) -> None:
    _assert_operators(vendor_clickhouse)


def test_snowflake_operators(vendor_snowflake: VendorTarget) -> None:
    _assert_operators(vendor_snowflake)


def test_bigquery_operators(vendor_bigquery: VendorTarget) -> None:
    _assert_operators(vendor_bigquery)


def test_databricks_operators(vendor_databricks: VendorTarget) -> None:
    _assert_operators(vendor_databricks)


def test_dremio_operators(vendor_dremio: VendorTarget) -> None:
    _assert_operators(vendor_dremio)
