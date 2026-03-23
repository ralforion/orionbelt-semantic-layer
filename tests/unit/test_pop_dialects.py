"""Tests for dialect-specific render_date_spine_cte_sql implementations."""

from __future__ import annotations

import pytest

from orionbelt.dialect.registry import DialectRegistry


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
        ("dremio", ["RECURSIVE", "spine_date", "spine_date_prev", "DATE_ADD"]),
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
