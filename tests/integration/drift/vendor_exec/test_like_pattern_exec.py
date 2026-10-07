"""LIKE patterns mean the same on every engine: case folding and escapes.

``ilike`` / ``notilike``: each dialect renders its own form (``ILIKE``,
Dremio's ``ILIKE()`` function, or ``LOWER`` of both sides on BigQuery and
MySQL). ClickHouse's ``LOWER`` folds ASCII only, so a ``LOWER`` fallback there
would miss ``Ä`` where its ``ILIKE`` does not.

Escapes: a backslash escapes ``%``, ``_`` and itself in OBML patterns. DuckDB,
Dremio and Snowflake read it literally unless told ``ESCAPE``; the others
read it so by default, and BigQuery rejects the clause.

This runs the rendering OBSL emits, not a hand-written spelling.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import CaseExpr, LikeMatch, Literal
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

#: (value, pattern, case_insensitive, matches)
CASES = [
    ("Mexico", "mex%", True, True),
    ("Mexico", "MEX%", True, True),
    ("mexico", "Mex%", True, True),
    ("Mexico", "x%", True, False),
    ("ÄRGER", "är%", True, True),
    ("ärger", "ÄR%", True, True),
    ("a_b", "a\\_b", False, True),
    ("axb", "a\\_b", False, False),
    ("A_B", "a\\_b", True, True),
    ("AxB", "a\\_b", True, False),
    ("100%", "100\\%", False, True),
    ("1000", "100\\%", False, False),
    ("a\\b", "a\\\\b", False, True),
    ("A\\B", "a\\\\b", True, True),
]


def _assert_like(target: VendorTarget) -> None:
    dialect = DialectRegistry.get(target.dialect)
    mismatches = []
    for value, pattern, case_insensitive, matches in CASES:
        for negated in (False, True):
            predicate = LikeMatch(
                column=Literal.string(value),
                pattern=pattern,
                negated=negated,
                case_insensitive=case_insensitive,
            )
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
    assert not mismatches, f"{target.name} like:\n  " + "\n  ".join(mismatches)


def test_duckdb_like_pattern(vendor_duckdb: VendorTarget) -> None:
    _assert_like(vendor_duckdb)


def test_postgres_like_pattern(vendor_postgres: VendorTarget) -> None:
    _assert_like(vendor_postgres)


def test_mysql_like_pattern(vendor_mysql: VendorTarget) -> None:
    _assert_like(vendor_mysql)


def test_clickhouse_like_pattern(vendor_clickhouse: VendorTarget) -> None:
    _assert_like(vendor_clickhouse)


def test_snowflake_like_pattern(vendor_snowflake: VendorTarget) -> None:
    _assert_like(vendor_snowflake)


def test_bigquery_like_pattern(vendor_bigquery: VendorTarget) -> None:
    _assert_like(vendor_bigquery)


def test_databricks_like_pattern(vendor_databricks: VendorTarget) -> None:
    _assert_like(vendor_databricks)
