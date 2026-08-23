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


def _assert_numeric_text_casts_to_its_value(target: VendorTarget) -> None:
    """A text source holding a number reads as that number under an integer cast.

    Narrow, and it earns its place. The #356 fix on ClickHouse first rendered
    ``accurateCast(trunc(x), ...)`` for every integer target, and ``trunc``
    refuses a String, so a measure aggregating a text column under
    ``dataType: integer`` raised code 43 where it had answered before. The
    second attempt went through ``toString`` and broke Bool and Date instead.
    The guard is scoped to numeric aggregates now, and this is what says so.

    Only the parseable case is asserted. Unparseable text is a genuine
    cross-engine divergence rather than a contract - DuckDB, PostgreSQL,
    BigQuery and Databricks raise where ClickHouse answers NULL and MySQL
    answers 0 - and pinning it here would assert a disagreement rather than an
    agreement.
    """
    dialect = DialectRegistry.get(target.dialect)
    rendered = dialect.compile_expr(
        dialect.cast_to_obml_type(RawSQL(sql="'42'"), parse_data_type("integer"))
    )
    rows = target.execute(f"SELECT {rendered} AS v")
    got = next(iter(rows[0].values()))
    assert got is not None and int(got) == 42, f"{target.name}: {rendered} returned {got!r}"


def test_duckdb_numeric_text_casts_to_its_value(vendor_duckdb: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_duckdb)


def test_postgres_numeric_text_casts_to_its_value(vendor_postgres: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_postgres)


def test_mysql_numeric_text_casts_to_its_value(vendor_mysql: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_mysql)


def test_clickhouse_numeric_text_casts_to_its_value(vendor_clickhouse: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_clickhouse)


def test_snowflake_numeric_text_casts_to_its_value(vendor_snowflake: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_snowflake)


def test_bigquery_numeric_text_casts_to_its_value(vendor_bigquery: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_bigquery)


def test_databricks_numeric_text_casts_to_its_value(vendor_databricks: VendorTarget) -> None:
    _assert_numeric_text_casts_to_its_value(vendor_databricks)


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
