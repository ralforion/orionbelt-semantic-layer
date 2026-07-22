"""Tests for dialect-specific render_date_spine_cte_sql implementations."""

from __future__ import annotations

import re

import pytest

from orionbelt.dialect.registry import DialectRegistry

ALL_DIALECTS = [
    "postgres",
    "duckdb",
    "snowflake",
    "bigquery",
    "databricks",
    "mysql",
    "clickhouse",
    "dremio",
]

# Date grains that reach period-over-period and relative-date arithmetic.
DATE_GRAINS = ["year", "quarter", "month", "week", "day"]


@pytest.mark.parametrize(
    "dialect_name,expected_keywords",
    [
        ("postgres", ["generate_series", "spine_date", "spine_date_prev"]),
        ("duckdb", ["generate_series", "spine_date", "spine_date_prev"]),
        ("snowflake", ["GENERATOR", "spine_date", "spine_date_prev", "DATEADD"]),
        ("bigquery", ["GENERATE_DATE_ARRAY", "spine_date", "spine_date_prev"]),
        ("databricks", ["EXPLODE", "SEQUENCE", "spine_date", "spine_date_prev"]),
        ("mysql", ["RECURSIVE", "spine_date", "spine_date_prev", "DATE_ADD"]),
        ("clickhouse", ["arrayJoin", "spine_date", "spine_date_prev"]),
        ("dremio", ["TIMESTAMPADD", "spine_date", "spine_date_prev", "CROSS JOIN"]),
    ],
)
def test_date_spine_sql_contains_keywords(dialect_name: str, expected_keywords: list[str]) -> None:
    dialect = DialectRegistry.get(dialect_name)
    sql = dialect.render_date_spine_cte_sql(
        min_date="date_range.min_date",
        max_date="date_range.max_date",
        grain="month",
        offset=-1,
        offset_grain="year",
    )
    for keyword in expected_keywords:
        assert keyword in sql, f"Expected '{keyword}' in {dialect_name} spine SQL:\n{sql}"


@pytest.mark.parametrize(
    "dialect_name",
    ["postgres", "duckdb", "snowflake", "bigquery", "databricks", "mysql", "clickhouse", "dremio"],
)
def test_date_spine_produces_two_columns(dialect_name: str) -> None:
    """All dialects must produce spine_date and spine_date_prev columns."""
    dialect = DialectRegistry.get(dialect_name)
    sql = dialect.render_date_spine_cte_sql(
        min_date="'2023-01-01'",
        max_date="'2024-01-01'",
        grain="month",
        offset=-1,
        offset_grain="year",
    )
    assert "spine_date" in sql.lower()
    assert "spine_date_prev" in sql.lower()


@pytest.mark.parametrize(
    "dialect_name",
    ["postgres", "duckdb", "snowflake", "bigquery", "databricks", "mysql", "clickhouse", "dremio"],
)
def test_date_trunc_sql(dialect_name: str) -> None:
    """All dialects must produce a valid date truncation expression."""
    dialect = DialectRegistry.get(dialect_name)
    result = dialect.render_date_trunc_sql("col", "month")
    assert "col" in result
    assert len(result) > 5  # basic sanity check


@pytest.mark.parametrize(
    "dialect_name",
    ["postgres", "duckdb", "snowflake", "bigquery", "databricks", "mysql", "clickhouse", "dremio"],
)
@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_date_spine_various_grains(dialect_name: str, grain: str) -> None:
    """Date spine generation should work for common grains."""
    dialect = DialectRegistry.get(dialect_name)
    sql = dialect.render_date_spine_cte_sql(
        min_date="'2023-01-01'",
        max_date="'2024-01-01'",
        grain=grain,
        offset=-1,
        offset_grain="year",
    )
    assert "spine_date" in sql.lower()


# ---------------------------------------------------------------------------
# Regression: quarter/week as the *offset* grain (the QoQ path).
#
# The bug: a period-over-period query at quarter grain (e.g. "Sales QoQ Ratio"
# with the "Sales Quarter" dimension) crashed or produced invalid SQL on four
# dialects, because the prior-period date is computed via ``date_add_sql`` with
# ``offset_grain="quarter"`` -- a code path no earlier test exercised (they all
# pinned ``offset_grain`` to year/month):
#   - Dremio    emitted ``DATE_ADD(d, INTERVAL '-1' QUARTER)`` (QUARTER/WEEK are
#               not valid Calcite interval qualifiers).
#   - Postgres  emitted ``d + INTERVAL '-1 quarter'`` ("invalid input syntax for
#               type interval").
#   - Databricks / ClickHouse raised ``ValueError`` (no quarter branch).
# sqlglot parses the invalid forms without complaint, so string/parse checks
# miss this -- these tests assert no exception, guard the specific bad output,
# and execute the generated spine on a live DuckDB engine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
@pytest.mark.parametrize("grain", DATE_GRAINS)
def test_date_add_sql_all_grains_no_raise(dialect_name: str, grain: str) -> None:
    """``date_add_sql`` must render every date grain without raising."""
    dialect = DialectRegistry.get(dialect_name)
    sql = dialect.date_add_sql("d", grain, -1)
    assert sql
    # Dremio interval qualifiers are YEAR/MONTH/DAY/HOUR/MINUTE/SECOND only --
    # QUARTER/WEEK must go through TIMESTAMPADD, never an INTERVAL literal.
    if dialect_name == "dremio":
        assert not re.search(r"INTERVAL\s+'-?\d+'\s+(QUARTER|WEEK)", sql, re.I), sql
    # Postgres interval input has no 'quarter' unit; it must be expressed as months.
    if dialect_name == "postgres" and grain == "quarter":
        assert "quarter" not in sql.lower(), sql


@pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
@pytest.mark.parametrize("offset_grain", DATE_GRAINS)
def test_date_spine_offset_grain_no_raise(dialect_name: str, offset_grain: str) -> None:
    """The spine must handle every grain as the *offset* grain, not just base."""
    dialect = DialectRegistry.get(dialect_name)
    sql = dialect.render_date_spine_cte_sql(
        min_date="'2023-01-01'",
        max_date="'2024-01-01'",
        grain=offset_grain,
        offset=-1,
        offset_grain=offset_grain,
    )
    assert "spine_date_prev" in sql.lower()


def test_dremio_previous_value_references_base_measure() -> None:
    """Dremio ``previousValue`` must reference ``pop_base`` to dodge a Dremio bug.

    Dremio miscompiles a self-joined CTE column projected on its own (reads the
    joined decimal's bytes as the output date, raising a monthOfYear range
    error). The fix adds a value-preserving reference to the base measure. Only
    Dremio needs it; every other dialect returns the prior value verbatim.
    """
    prev, current = 'pop_prev."m"', 'pop_base."m"'
    dremio_sql = DialectRegistry.get("dremio").render_pop_previous_value_sql(prev, current)
    assert current in dremio_sql and prev in dremio_sql
    for other in [d for d in ALL_DIALECTS if d != "dremio"]:
        assert DialectRegistry.get(other).render_pop_previous_value_sql(prev, current) == prev


@pytest.mark.parametrize("grain", ["quarter", "week", "month", "year"])
def test_quarter_week_spine_executes_on_duckdb(grain: str) -> None:
    """The generated DuckDB spine must be valid SQL a real engine accepts."""
    duckdb = pytest.importorskip("duckdb")
    dialect = DialectRegistry.get("duckdb")
    body = dialect.render_date_spine_cte_sql(
        min_date="DATE '2020-01-01'",
        max_date="DATE '2021-12-31'",
        grain=grain,
        offset=-1,
        offset_grain=grain,
    )
    rows = duckdb.sql(f"WITH date_spine AS ({body}) SELECT * FROM date_spine").fetchall()
    assert rows, f"empty {grain} spine"
