"""``ilike`` / ``notilike`` match case-insensitively on every engine.

Each dialect renders its own form (``ILIKE``, Dremio's ``ILIKE()`` function, or
``LOWER`` of both sides on BigQuery and MySQL). This runs the rendering OBSL
emits, not a hand-written spelling: ClickHouse's ``LOWER`` folds ASCII only,
so a ``LOWER`` fallback there would miss ``Ä`` where its ``ILIKE`` does not.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import CaseExpr, ILikeMatch, Literal
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

#: (value, pattern, matches)
CASES = [
    ("Mexico", "mex%", True),
    ("Mexico", "MEX%", True),
    ("mexico", "Mex%", True),
    ("Mexico", "x%", False),
    ("ÄRGER", "är%", True),
    ("ärger", "ÄR%", True),
]


def _assert_ilike(target: VendorTarget) -> None:
    dialect = DialectRegistry.get(target.dialect)
    mismatches = []
    for value, pattern, matches in CASES:
        for negated in (False, True):
            predicate = ILikeMatch(column=Literal.string(value), pattern=pattern, negated=negated)
            sql = dialect.compile_expr(
                CaseExpr(
                    when_clauses=[(predicate, Literal.number(1))],
                    else_clause=Literal.number(0),
                )
            )
            expected = int(matches != negated)
            try:
                got = next(iter(target.execute(f"SELECT {sql} AS v")[0].values()))
            except Exception as exc:  # noqa: BLE001 - a raise is a failure like any other
                mismatches.append(f"{sql} -> raised {str(exc)[:80]}")
                continue
            if int(got) != expected:
                mismatches.append(f"{sql} -> {got!r}, expected {expected}")
    assert not mismatches, f"{target.name} ilike:\n  " + "\n  ".join(mismatches)


def test_duckdb_ilike(vendor_duckdb: VendorTarget) -> None:
    _assert_ilike(vendor_duckdb)


def test_postgres_ilike(vendor_postgres: VendorTarget) -> None:
    _assert_ilike(vendor_postgres)


def test_mysql_ilike(vendor_mysql: VendorTarget) -> None:
    _assert_ilike(vendor_mysql)


def test_clickhouse_ilike(vendor_clickhouse: VendorTarget) -> None:
    _assert_ilike(vendor_clickhouse)


def test_snowflake_ilike(vendor_snowflake: VendorTarget) -> None:
    _assert_ilike(vendor_snowflake)


def test_bigquery_ilike(vendor_bigquery: VendorTarget) -> None:
    _assert_ilike(vendor_bigquery)


def test_databricks_ilike(vendor_databricks: VendorTarget) -> None:
    _assert_ilike(vendor_databricks)
