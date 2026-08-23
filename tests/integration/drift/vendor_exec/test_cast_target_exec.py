"""Every OBML type this dialect can render must be a legal CAST target.

``render_obml_type`` feeds ``cast_to_obml_type``, which is what a measure's
``dataType`` compiles through. A spelling that is a valid *column* type but not
a valid *cast* target produces invalid SQL from a model that validates clean,
and nothing upstream catches it: the OBML validator does not know the
vocabulary, and the sqlglot post-check is non-blocking.

MySQL had two of them (#357). ``TIMESTAMP`` and ``TINYINT(1)`` are both legal
MySQL column types and neither is in the ``CAST`` vocabulary, so
``dataType: timestamp`` and ``dataType: boolean`` compiled to error 1064. The
dialect already rewrote ``VARCHAR`` for exactly this reason; these two were
missed because no test executed a cast to them.

This asserts the general contract rather than those two cases, so a new type or
a new dialect cannot reintroduce the shape.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import RawSQL
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.types import parse_data_type

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

# Every type ``parse_data_type`` accepts, with a source value that is plausible
# for it so a failure is the target's fault rather than the input's.
TARGETS: list[tuple[str, str]] = [
    ("string", "1"),
    ("integer", "1"),
    ("bigint", "1"),
    ("double", "1"),
    ("decimal(18, 2)", "1"),
    ("date", "'2026-08-15'"),
    ("time", "'13:45:00'"),
    ("timestamp", "'2026-08-15 13:45:00'"),
    ("boolean", "1"),
]


def _assert_every_target_executes(target: VendorTarget) -> None:
    dialect = DialectRegistry.get(target.dialect)
    failures = []
    for type_name, source in TARGETS:
        rendered = dialect.compile_expr(
            dialect.cast_to_obml_type(RawSQL(sql=source), parse_data_type(type_name))
        )
        try:
            target.execute(f"SELECT {rendered} AS v")
        except Exception as exc:  # noqa: BLE001 - the error text is the finding
            detail = str(exc).splitlines()[0]
            failures.append(f"dataType {type_name!r} rendered {rendered} and failed: {detail}")
    assert not failures, f"{target.name}:\n  " + "\n  ".join(failures)


def test_duckdb_every_cast_target_executes(vendor_duckdb: VendorTarget) -> None:
    _assert_every_target_executes(vendor_duckdb)


def test_postgres_every_cast_target_executes(vendor_postgres: VendorTarget) -> None:
    _assert_every_target_executes(vendor_postgres)


def test_mysql_every_cast_target_executes(vendor_mysql: VendorTarget) -> None:
    _assert_every_target_executes(vendor_mysql)


def test_clickhouse_every_cast_target_executes(vendor_clickhouse: VendorTarget) -> None:
    _assert_every_target_executes(vendor_clickhouse)


def test_snowflake_every_cast_target_executes(vendor_snowflake: VendorTarget) -> None:
    _assert_every_target_executes(vendor_snowflake)


def test_bigquery_every_cast_target_executes(vendor_bigquery: VendorTarget) -> None:
    _assert_every_target_executes(vendor_bigquery)


def test_databricks_every_cast_target_executes(vendor_databricks: VendorTarget) -> None:
    _assert_every_target_executes(vendor_databricks)
