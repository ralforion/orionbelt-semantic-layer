"""Unit tests for pgwire/router.py:references_catalog detection helper."""

from __future__ import annotations

import pytest

from orionbelt.pgwire.router import references_catalog


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_catalog.pg_class",
        (
            "select c.relname FROM pg_catalog.pg_class c "
            "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        ),
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        "SELECT pg_catalog.set_config('search_path', 'public', false)",
        # Mixed-case variants — Postgres clients sometimes emit caps.
        "SELECT * FROM PG_CATALOG.PG_CLASS",
    ],
)
def test_references_catalog_positive(sql: str) -> None:
    assert references_catalog(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        'SELECT "Region", "Total Sales" FROM commerce',
        "SELECT count(*) FROM orders WHERE created_at > '2024-01-01'",
        "SHOW server_version",
        "SET extra_float_digits = 3",
    ],
)
def test_references_catalog_negative(sql: str) -> None:
    assert references_catalog(sql) is False


def test_references_catalog_falls_back_on_parse_error() -> None:
    """Unparseable SQL with catalog tokens still routes to the catalog branch."""

    sql = "SELECT FROM PG_CATALOG.PG_ATTRIBUTE WHERE ;;;"
    assert references_catalog(sql) is True


def test_references_catalog_no_false_positive_on_string_literal() -> None:
    """A literal containing 'pg_catalog' inside a string must not trigger."""

    # The fallback substring scan only fires when sqlglot can't parse,
    # so this query (well-formed) goes through the AST path and the
    # literal stays out of the catalog branch.
    sql = "SELECT 'pg_catalog is a real schema' AS note"
    assert references_catalog(sql) is False
