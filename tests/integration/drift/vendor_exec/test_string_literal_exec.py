"""A string literal survives the round trip, on every engine.

``compile_expr`` doubled the quote and left the backslash alone, which is the
SQL standard and right on two engines out of seven. Measured with that
rendering:

===========  ===================================  ==========================
engine       ``a\\b``                              ``it's``
===========  ===================================  ==========================
DuckDB       ok                                   ok
PostgreSQL   ok                                   ok
MySQL        ``a\\x08`` - a backspace, silently    ok
ClickHouse   ``a\\x08``, and ``C:\\temp\\x`` raises  ok
Snowflake    ``a\\x08``, and ``C:\\temp\\x`` raises  ok
BigQuery     ``a\\x08``, and ``C:\\temp\\x`` raises  **raises** - reads it as
                                                  two concatenated literals
Databricks   ``a\\x08``                             **``its``**, silently
===========  ===================================  ==========================

So a Windows path, a regex or an escaped delimiter in a filter was silently
wrong on five engines, and an apostrophe - the one case the old code was
written for - was wrong on two, one of them silently.

Both conventions are *wrong* on the other side rather than merely unnecessary,
which is why this is a dialect property and not a wider escape.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import Literal
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

#: Values a model could legitimately filter on. The apostrophe is here because
#: the old rendering handled it and still got it wrong on two engines.
VALUES = [
    "plain",
    "it's",
    'quote"double',
    "a\\b",
    "C:\\temp\\x",
    "back\\\\slash",
    "tab\\there",
    "percent%_underscore",
    "unicode: é ü 中",
]


def _assert_round_trip(target: VendorTarget) -> None:
    dialect = DialectRegistry.get(target.dialect)
    mismatches = []
    for raw in VALUES:
        literal = dialect.compile_expr(Literal.string(raw))
        try:
            got = next(iter(target.execute(f"SELECT {literal} AS v")[0].values()))
        except Exception as exc:  # noqa: BLE001 - a raise is a failure like any other
            mismatches.append(f"{raw!r} -> raised {str(exc)[:60]}")
            continue
        if got != raw:
            mismatches.append(f"{raw!r} -> {got!r} (emitted {literal})")
    assert not mismatches, f"{target.name} mangles string literals:\n  " + "\n  ".join(mismatches)


def test_duckdb_string_literals(vendor_duckdb: VendorTarget) -> None:
    _assert_round_trip(vendor_duckdb)


def test_postgres_string_literals(vendor_postgres: VendorTarget) -> None:
    _assert_round_trip(vendor_postgres)


def test_mysql_string_literals(vendor_mysql: VendorTarget) -> None:
    _assert_round_trip(vendor_mysql)


def test_clickhouse_string_literals(vendor_clickhouse: VendorTarget) -> None:
    _assert_round_trip(vendor_clickhouse)


def test_bigquery_string_literals(vendor_bigquery: VendorTarget) -> None:
    _assert_round_trip(vendor_bigquery)


def test_snowflake_string_literals(vendor_snowflake: VendorTarget) -> None:
    _assert_round_trip(vendor_snowflake)


def test_databricks_string_literals(vendor_databricks: VendorTarget) -> None:
    _assert_round_trip(vendor_databricks)
