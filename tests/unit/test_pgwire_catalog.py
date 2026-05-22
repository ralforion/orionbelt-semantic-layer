"""Unit tests for pgwire/catalog.py — CatalogEmulator (DuckDB-backed)."""

from __future__ import annotations

import pytest

from orionbelt.pgwire.catalog import CatalogEmulator
from orionbelt.service.session_manager import SessionManager
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def manager_with_model() -> SessionManager:
    mgr = SessionManager()
    store = mgr.get_or_create_named("commerce")
    store.load_model(SAMPLE_MODEL_YAML)
    return mgr


def test_refresh_creates_one_table_per_model(manager_with_model: SessionManager) -> None:
    """v2.5.0 layout: database=orionbelt, schema=<model>, table='model'."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY 1, 2"
    )
    rows = [(row[0], row[1]) for row in result.rows]
    assert ("commerce", "model") in rows


def test_table_has_expected_columns(manager_with_model: SessionManager) -> None:
    """All model dimensions, measures, and metrics are exposed as columns."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='commerce' AND table_name='model' ORDER BY ordinal_position"
    )
    columns = [row[0] for row in result.rows]
    # SAMPLE_MODEL_YAML exposes one dim, three measures, and two metrics.
    assert "Customer Country" in columns
    assert "Total Revenue" in columns
    assert "Order Count" in columns
    assert "Grand Total Revenue" in columns
    assert "Revenue per Order" in columns
    assert "Revenue Share" in columns


def test_refresh_is_idempotent(manager_with_model: SessionManager) -> None:
    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT count(*) FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind='r' AND n.nspname='commerce' AND c.relname='model'"
    )
    assert result.rows[0][0] == 1


def test_refresh_drops_stale_models() -> None:
    """A model removed from the SessionManager disappears from the catalog."""

    mgr = SessionManager()
    store = mgr.get_or_create_named("temp_model")
    store.load_model(SAMPLE_MODEL_YAML)
    emu = CatalogEmulator()
    emu.refresh(mgr)
    # Reset SessionManager to empty and refresh.
    empty = SessionManager()
    emu.refresh(empty)
    result = emu.execute("SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname='temp_model'")
    assert result.rows[0][0] == 0


def test_psql_dt_style_query_returns_rows(manager_with_model: SessionManager) -> None:
    """\\dt's actual SQL must surface the model table at <model>.model."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    sql = (
        "SELECT n.nspname AS schema, c.relname AS name "
        "FROM pg_catalog.pg_class c "
        "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r','p','') "
        "AND n.nspname NOT IN ('pg_catalog','information_schema') "
        "ORDER BY 1,2"
    )
    result = emu.execute(sql)
    rows = [(row[0], row[1]) for row in result.rows]
    assert ("commerce", "model") in rows


def test_empty_session_manager_yields_no_tables() -> None:
    emu = CatalogEmulator()
    emu.refresh(SessionManager())
    result = emu.execute("SELECT relname FROM pg_catalog.pg_class WHERE relkind='r'")
    assert result.rows == []


def test_shadow_views_hidden_from_get_tables(
    manager_with_model: SessionManager,
) -> None:
    """Tableau's getTables query lists every visible table/view in the
    schema browser. The shadow views (_obsl_pg_attribute / _obsl_pg_type)
    are implementation details and must not show up there.

    They're created as ``TEMP VIEW`` — DuckDB puts temp objects in a
    catalog where ``pg_class.relnamespace`` is NULL, so the
    ``WHERE nspname NOT IN ('pg_catalog', 'information_schema')`` filter
    in pgjdbc's getTables excludes them (``NULL NOT IN (…)`` is NULL,
    which the WHERE drops).
    """

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT n.nspname, c.relname, c.relkind "
        "FROM pg_catalog.pg_namespace n "
        "JOIN pg_catalog.pg_class c ON (c.relnamespace = n.oid) "
        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND c.relkind IN ('r', 'v') "
        "ORDER BY 1, 2"
    )
    visible_pairs = {(row[0], row[1]) for row in result.rows}
    # Shadow views must be hidden (TEMP scope → relnamespace IS NULL).
    visible_names = {pair[1] for pair in visible_pairs}
    assert "_obsl_pg_attribute" not in visible_names
    assert "_obsl_pg_type" not in visible_names
    # The user-facing model table is still listed at <schema>.model.
    assert ("commerce", "model") in visible_pairs


def test_pg_attribute_atttypid_returns_real_postgres_oids(
    manager_with_model: SessionManager,
) -> None:
    """Tableau's pgjdbc reads pg_attribute.atttypid to learn each column's
    Postgres OID. DuckDB's native pg_attribute stores DuckDB internal
    type ids (DOUBLE → 23, BIGINT → 14, DATE → 15), which collide with
    different Postgres OIDs (Postgres 23 = INT4). The shadow view +
    rewrite translate to real Postgres OIDs so a JOIN with pg_type works
    and pgjdbc allocates the right-width column reader. Without this
    fix Tableau measures arrive as NULL / 0 because the 8-byte FLOAT8
    wire bytes get parsed as an INT4.
    """

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT a.attname, a.atttypid, t.typname "
        "FROM pg_attribute a "
        "JOIN pg_class c ON a.attrelid = c.oid "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "JOIN pg_type t ON a.atttypid = t.oid "
        "WHERE n.nspname = 'commerce' AND c.relname = 'model' AND a.attnum > 0 "
        "ORDER BY a.attnum"
    )
    by_name = {row[0]: (row[1], row[2]) for row in result.rows}
    # Total Revenue is a measure with DOUBLE backing — must surface as
    # FLOAT8 (OID 701) so pgjdbc allocates an 8-byte Double reader.
    assert by_name["Total Revenue"][0] == 701  # FLOAT8
    assert by_name["Total Revenue"][1] == "float8"
    assert by_name["Order Count"][0] == 20  # INT8 (BIGINT)
    assert by_name["Customer Country"][0] == 25  # TEXT


def test_pg_expandarray_resolves_for_jdbc_get_primary_keys(
    manager_with_model: SessionManager,
) -> None:
    """JDBC's getPrimaryKeys query references ``information_schema._pg_expandarray``.

    DuckDB doesn't ship that helper. We add a stub macro + a SQL
    rewrite that strips the ``information_schema.`` prefix so the
    function resolves. Tableau hit this during connect-check.
    """

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    # The shape pgjdbc actually emits — works on a literal array
    # because our virtual tables have no indexes for the FROM side.
    result = emu.execute("SELECT (information_schema._pg_expandarray(ARRAY[1,2,3])).n AS key_seq")
    # Stub returns a STRUCT with NULL fields — only needs to resolve.
    assert list(result.rows[0]) == [None]
