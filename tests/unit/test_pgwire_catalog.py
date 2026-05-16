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
    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT relname FROM pg_catalog.pg_class WHERE relkind='r' ORDER BY relname"
    )
    table_names = [row[0] for row in result.rows]
    assert "commerce" in table_names


def test_table_has_expected_columns(manager_with_model: SessionManager) -> None:
    """All model dimensions, measures, and metrics are exposed as columns."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='commerce' ORDER BY ordinal_position"
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
    result = emu.execute("SELECT count(*) FROM pg_catalog.pg_class WHERE relname='commerce'")
    assert result.rows[0][0] == 1


def test_refresh_drops_stale_models() -> None:
    """A model removed from the SessionManager disappears from the catalog."""

    mgr = SessionManager()
    store = mgr.get_or_create_named("temp")
    store.load_model(SAMPLE_MODEL_YAML)
    emu = CatalogEmulator()
    emu.refresh(mgr)
    # Reset SessionManager to empty and refresh.
    empty = SessionManager()
    emu.refresh(empty)
    result = emu.execute("SELECT count(*) FROM pg_catalog.pg_class WHERE relname='temp'")
    assert result.rows[0][0] == 0


def test_psql_dt_style_query_returns_rows(manager_with_model: SessionManager) -> None:
    """\\dt's actual SQL must surface the model table."""

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
    names = [row[1] for row in result.rows]
    assert "commerce" in names


def test_empty_session_manager_yields_no_tables() -> None:
    emu = CatalogEmulator()
    emu.refresh(SessionManager())
    result = emu.execute("SELECT relname FROM pg_catalog.pg_class WHERE relkind='r'")
    assert result.rows == []
