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
        # Bare ``pg_*`` references — DBeaver / pgAdmin emit these without
        # the schema qualifier.
        "SELECT count(*) FROM pg_description",
        (
            "SELECT count(*) FROM pg_description d, pg_namespace n "
            "WHERE d.objoid=n.oid AND d.classoid='pg_namespace'::regclass"
        ),
    ],
)
def test_references_catalog_positive(sql: str) -> None:
    assert references_catalog(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        # Real-world semantic queries with a model FROM target.
        'SELECT "Region", "Total Sales" FROM commerce',
        "SELECT count(*) FROM orders WHERE created_at > '2024-01-01'",
        "SHOW server_version",
        "SET extra_float_digits = 3",
        # No-FROM SELECTs of plain literals / bare column refs are
        # *not* catalog probes — they belong on the canned (literal 1)
        # or semantic (bare dimension) path. Only system functions
        # and named system identifiers route to the catalog branch.
        "SELECT 1",
        'SELECT "Customer Country"',
        "SELECT 'pg_catalog is a real schema' AS note",
    ],
)
def test_references_catalog_negative(sql: str) -> None:
    assert references_catalog(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        # DBeaver's refreshDefaults emits SELECTs with system info
        # functions / identifiers and no FROM clause. These belong on
        # the catalog branch so DuckDB computes them natively; OBSQL
        # would reject because there's no model reference.
        "SELECT current_schema(), session_user",
        "SELECT version()",
        "SELECT current_user",
        "SELECT session_user, current_database()",
    ],
)
def test_references_catalog_no_from_routes_to_catalog(sql: str) -> None:
    assert references_catalog(sql) is True


def test_references_catalog_falls_back_on_parse_error() -> None:
    """Unparseable SQL with catalog tokens still routes to the catalog branch."""

    sql = "SELECT FROM PG_CATALOG.PG_ATTRIBUTE WHERE ;;;"
    assert references_catalog(sql) is True
