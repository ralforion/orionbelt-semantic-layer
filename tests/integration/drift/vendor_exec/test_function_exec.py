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

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.ast.nodes import Cast, InTimeZone, Literal
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.functions import FUNCTION_CATALOG, FunctionSpec
from orionbelt.models.semantic import WeekStart

from ._catalog_values import matches as _matches
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

CATALOG = list(FUNCTION_CATALOG.values())


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


# ---------------------------------------------------------------------------
# settings.weekStart — the one catalog rule a model can change
# ---------------------------------------------------------------------------

_WEEK_CASES: list[tuple[str, str, str | int]] = [
    # 2026-08-15 is a Saturday. Its Monday is the 10th, its Sunday the 9th.
    ("date_trunc('week', DATE '2026-08-15')", "monday", "2026-08-10"),
    ("date_trunc('week', DATE '2026-08-15')", "sunday", "2026-08-09"),
    # 2026-08-09 is a Sunday, so it opens a week under one calendar and closes
    # the previous one under the other: the difference to the 15th differs.
    ("date_diff('week', DATE '2026-08-09', DATE '2026-08-15')", "monday", 1),
    ("date_diff('week', DATE '2026-08-09', DATE '2026-08-15')", "sunday", 0),
    # A timestamp input: the start of a week is midnight, so a rewrite that
    # subtracts days from the value rather than from its day keeps 13:45 and
    # fails here. Snowflake and Dremio did exactly that.
    ("date_trunc('week', TIMESTAMP '2026-08-15 13:45:00')", "monday", "2026-08-10"),
    ("date_trunc('week', TIMESTAMP '2026-08-15 13:45:00')", "sunday", "2026-08-09"),
]


def _assert_week_start(vendor: VendorTarget) -> None:
    """Execute both calendars, rather than trusting either engine's default."""
    engine = DialectRegistry.get(vendor.dialect)
    failures = []
    for call, week_start, expected in _WEEK_CASES:
        engine.week_start = WeekStart(week_start)
        ast = parse_expression(tokenize_metric_formula(call))
        sql = f"SELECT {engine.compile_expr(ast)} AS c0"
        actual = next(iter(vendor.execute(sql)[0].values()))
        if not _matches(expected, actual):
            failures.append(f"{call} under weekStart={week_start}: {actual!r} != {expected!r}")
    assert not failures, f"{vendor.name}:\n  " + "\n  ".join(failures)


def test_duckdb_week_start(vendor_duckdb: VendorTarget) -> None:
    _assert_week_start(vendor_duckdb)


def test_postgres_week_start(vendor_postgres: VendorTarget) -> None:
    _assert_week_start(vendor_postgres)


def test_mysql_week_start(vendor_mysql: VendorTarget) -> None:
    _assert_week_start(vendor_mysql)


def test_clickhouse_week_start(vendor_clickhouse: VendorTarget) -> None:
    _assert_week_start(vendor_clickhouse)


def test_snowflake_week_start(vendor_snowflake: VendorTarget) -> None:
    _assert_week_start(vendor_snowflake)


def test_bigquery_week_start(vendor_bigquery: VendorTarget) -> None:
    _assert_week_start(vendor_bigquery)


def test_databricks_week_start(vendor_databricks: VendorTarget) -> None:
    _assert_week_start(vendor_databricks)


# ---------------------------------------------------------------------------
# settings.queryTimezone — the frame every timestamp column is read in
# ---------------------------------------------------------------------------

# The instant 2026-08-09 22:30 UTC is 00:30 on Monday the 10th in Zagreb, so a
# conversion that works moves the value across a day *and* a week boundary.
_TZ_INSTANT = "2026-08-09 22:30:00"
_TZ_EXPECTED_WALL_CLOCK = "2026-08-10 00:30:00"


def _tz_node() -> InTimeZone:
    return InTimeZone(
        expr=Cast(expr=Literal.string(_TZ_INSTANT), type_name="timestamp"),
        zone="Europe/Zagreb",
        from_zone="UTC",
    )


def _assert_timezone_conversion(vendor: VendorTarget) -> None:
    """A naive UTC timestamp read in the model's zone, executed."""
    engine = DialectRegistry.get(vendor.dialect)
    sql = f"SELECT {engine.compile_expr(_tz_node())} AS c0"
    actual = next(iter(vendor.execute(sql)[0].values()))
    # Engines differ on whether the result carries an offset; the wall clock is
    # what the model asked for.
    assert str(actual)[:19] == _TZ_EXPECTED_WALL_CLOCK, f"{vendor.name}: {actual!r}\nSQL: {sql}"


def test_duckdb_query_timezone(vendor_duckdb: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_duckdb)


def test_postgres_query_timezone(vendor_postgres: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_postgres)


def test_mysql_query_timezone(vendor_mysql: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_mysql)


def test_clickhouse_query_timezone(vendor_clickhouse: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_clickhouse)


def test_snowflake_query_timezone(vendor_snowflake: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_snowflake)


def test_bigquery_query_timezone(vendor_bigquery: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_bigquery)


def test_databricks_query_timezone(vendor_databricks: VendorTarget) -> None:
    _assert_timezone_conversion(vendor_databricks)
