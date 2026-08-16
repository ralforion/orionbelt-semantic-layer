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

import re
from typing import Any

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.functions import FUNCTION_CATALOG, FunctionSpec
from orionbelt.models.semantic import WeekStart

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

CATALOG = list(FUNCTION_CATALOG.values())


_NUMERIC_TOLERANCE = 1e-9
"""Relative tolerance for a numeric catalog value.

Not laxity about the answer: the catalog's numeric entries are floating point,
and an engine is free to deliver 2.35 as ``Decimal('2.3500')`` or the base
change behind ``log(2, 8)`` as 2.9999999999999996. What the catalog pins is the
value, so the comparison is numeric rather than a string match, which would
fail on the scale a driver happened to choose. It is tight enough that a real
disagreement (2 against 3 for ``round(2.5)``, -3 against -4 for ``div(-7, 2)``)
still fails.
"""


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _matches_date(expected: str, actual: Any) -> bool:
    """Whether a date-valued entry returned the documented calendar day.

    ``date_trunc`` comes back as a DATE on ClickHouse, Snowflake and MySQL and
    as a TIMESTAMP at midnight on DuckDB and Postgres. The catalog pins the
    instant, not which of the two an engine chose, so the day is compared and a
    time component is required to be midnight rather than ignored.
    """
    if not hasattr(actual, "isoformat"):
        return False
    if actual.isoformat()[:10] != expected:
        return False
    time = getattr(actual, "time", None)
    return time is None or time().isoformat().startswith("00:00:00")


def _matches(expected: str | int | float | bool | None, actual: Any) -> bool:
    """Whether *actual* is the documented value, across driver type mappings.

    Booleans come back as ``1``/``0`` from MySQL and ClickHouse, numbers as
    ``Decimal`` or ``float`` depending on the driver, and strings are strings
    everywhere — so each expected type is compared in its own terms rather than
    by equality on whatever Python object the driver chose.
    """
    if expected is None:
        return actual is None
    if actual is None:
        return False
    if isinstance(expected, bool):
        return bool(actual) is expected
    if isinstance(expected, (int, float)):
        return abs(float(actual) - expected) <= _NUMERIC_TOLERANCE * max(1.0, abs(expected))
    if isinstance(expected, str) and _ISO_DATE.fullmatch(expected):
        return _matches_date(expected, actual)
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
