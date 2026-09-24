"""The pgwire catalog connection runs the catalog and OBSL objects, nothing else."""

from __future__ import annotations

import asyncio

import pytest

from orionbelt.pgwire.catalog_guard import catalog_rejection
from orionbelt.pgwire.router import SemanticRouter
from tests.unit.test_pgwire_router import _make_manager_with_model, _parse_frames

MODELS = {"commerce"}

#: Probes BI clients send (recorded from the pgwire suites) and OBSL browsing.
ACCEPTED = [
    "SELECT oid, nspname\nFROM pg_namespace\n\nORDER BY oid",
    "SELECT n.nspname, c.relname, c.relkind FROM pg_catalog.pg_class c "
    "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p')",
    "SELECT pg_namespace.oid, tablename, indexname\nFROM pg_indexes\n"
    "JOIN pg_namespace ON (schemaname=nspname)\n\nORDER BY pg_namespace.oid",
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'commerce' AND table_name = 'model' ORDER BY ordinal_position",
    "SELECT version(), (SELECT COUNT(*) FROM pg_settings WHERE name LIKE 'rds%')",
    "SELECT to_regclass('duckdb_secrets')",
    "SELECT 'hello'::text AS value",
    "SELECT * FROM pg_get_keywords()",
    "WITH s AS (SELECT nspname FROM pg_namespace) SELECT * FROM s",
    "SELECT * FROM commerce.model WHERE 1=0",
    'SELECT * FROM "orionbelt"."commerce"."dimensions"',
    "SELECT * FROM commerce._measures_metadata",
    "SELECT name FROM metrics",
    "WITH c AS (SELECT nspname FROM pg_namespace) SELECT x.nspname FROM c AS x",
    "WITH a AS (SELECT nspname FROM pg_namespace), b AS (SELECT * FROM a) SELECT * FROM b",
    "SELECT nspname FROM pg_namespace "
    "UNION ALL SELECT schema_name FROM information_schema.schemata",
    'CREATE TEMP TABLE "#probe" (a INTEGER)',
    'INSERT INTO "#probe" VALUES (1)',
    'SELECT a FROM "#probe"',
    'DROP TABLE "#probe"',
]

REJECTED = [
    # Review findings (allowlist bypasses): a LATERAL table function is a
    # source of its own node type, and CTE names must be resolved by scope.
    "SELECT * FROM pg_namespace, LATERAL duckdb_settings()",
    "SELECT * FROM pg_namespace CROSS JOIN LATERAL query_table('secret')",
    "SELECT * FROM query('SELECT * FROM secret')",
    "SELECT * FROM read_csv('/etc/passwd')",
    "WITH unused AS (WITH sqlite_master AS (SELECT 1) SELECT 1) SELECT * FROM sqlite_master",
    'INSERT INTO "#probe" SELECT * FROM sqlite_master',
    "WITH sqlite_master AS (SELECT 1) "
    "SELECT s.type FROM main.sqlite_master AS s CROSS JOIN pg_namespace LIMIT 1",
    "WITH sqlite_master AS (SELECT 1) SELECT * FROM sqlite_master AS s, main.sqlite_master",
    "SELECT * FROM commerce.not_an_obsl_object",
    "SELECT * FROM warehouse.sales",
    "SELECT * FROM sales",
    "SELECT * FROM other_db.pg_catalog.pg_class",
    "SELECT 1; SELECT 2",
    'SELECT 1; DROP TABLE "#probe"',
    "DELETE FROM pg_namespace",
    "UPDATE pg_settings SET setting = 'x'",
    "CREATE TABLE kept (a INTEGER)",
    "CREATE VIEW v AS SELECT 1",
    "DROP TABLE commerce.model",
    'DROP TABLE "#probe", commerce.model',
    'INSERT INTO commerce.model SELECT * FROM "#probe"',
    "SELECT * FROM some_table_function()",
    "SET search_path = x; SELECT 1",
    "this is not sql",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_catalog_and_obsl_queries_are_accepted(sql: str) -> None:
    assert catalog_rejection(sql, MODELS) is None


@pytest.mark.parametrize("sql", REJECTED)
def test_everything_else_is_rejected(sql: str) -> None:
    assert catalog_rejection(sql, MODELS) is not None


def test_obsl_objects_of_an_unloaded_model_are_rejected() -> None:
    assert catalog_rejection("SELECT * FROM retired.model", MODELS) is not None


def test_router_refuses_a_non_catalog_relation_in_a_model_schema() -> None:
    """Routed to the catalog by its model-schema qualifier, then refused there."""
    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    frames = _parse_frames(
        asyncio.run(router.handle("SELECT * FROM commerce.not_an_obsl_object", database="commerce"))
    )
    errors = [body for tag, body in frames if tag == b"E"]
    assert errors and b"CATALOG_QUERY_REJECTED" in errors[0] and b"42501" in errors[0]


def test_router_still_answers_obsl_metadata() -> None:
    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    frames = _parse_frames(
        asyncio.run(router.handle("SELECT name FROM commerce.dimensions", database="commerce"))
    )
    tags = [tag for tag, _ in frames]
    assert b"E" not in tags and b"D" in tags
