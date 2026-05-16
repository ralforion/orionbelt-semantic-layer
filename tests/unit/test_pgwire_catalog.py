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


# ---------------------------------------------------------------------------
# Step 5 — obsl_meta views fixing the psql 16 \d gap
# ---------------------------------------------------------------------------


def test_obsl_meta_pg_class_has_psql16_columns(
    manager_with_model: SessionManager,
) -> None:
    """pg_class probe receives the columns psql 16's \\d expects."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT relname, relforcerowsecurity, relrowsecurity, relhasoids, "
        "relispartition, relreplident, relpersistence "
        "FROM pg_catalog.pg_class WHERE relname='commerce'"
    )
    assert result.row_count == 1
    row = result.rows[0]
    assert row[0] == "commerce"
    # All Boolean defaults are false; replident / persistence are
    # one-character strings; no NULLs anywhere.
    assert row[1] is False
    assert row[6] == "p"


def test_obsl_meta_pg_attribute_returns_real_postgres_oids(
    manager_with_model: SessionManager,
) -> None:
    """atttypid now matches real Postgres OIDs (was DuckDB-internal in Step 3)."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT a.attname, a.atttypid "
        "FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "WHERE c.relname='commerce' ORDER BY a.attnum"
    )
    types = {row[0]: row[1] for row in result.rows}
    # SAMPLE_MODEL_YAML's "Customer Country" is a string dimension → OID 1043 (varchar).
    assert types.get("Customer Country") == 1043
    # "Total Revenue" / "Order Count" are floats → OID 701 (float8).
    assert types.get("Total Revenue") == 701


def test_refresh_preserves_pg_class_oid_across_calls(
    manager_with_model: SessionManager,
) -> None:
    """Stable refresh: same model → same oid (psql \\d's two-step probe)."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    oid_before = emu.execute("SELECT oid FROM pg_catalog.pg_class WHERE relname='commerce'").rows[
        0
    ][0]
    emu.refresh(manager_with_model)
    oid_after = emu.execute("SELECT oid FROM pg_catalog.pg_class WHERE relname='commerce'").rows[0][
        0
    ]
    assert oid_before == oid_after


def test_dbeaver_regclass_cast_does_not_error(
    manager_with_model: SessionManager,
) -> None:
    """Bare ``::regclass`` casts (DBeaver's pg_description probe) round-trip.

    DuckDB has no ``regclass`` type; the rewriter collapses the cast to
    ``::VARCHAR``. The surrounding probe runs against an empty
    pg_description stub so the integer-vs-string comparison filters out
    everything without erroring.
    """

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT count(*) FROM pg_description d, pg_namespace n "
        "WHERE d.objoid = n.oid AND d.objsubid = 0 "
        "AND d.classoid = 'pg_namespace'::regclass"
    )
    assert result.row_count == 1


def test_dbeaver_pg_database_probe_returns_full_columns(
    manager_with_model: SessionManager,
) -> None:
    """DBeaver's ``SELECT db.oid, db.* FROM pg_database WHERE datallowconn``.

    DuckDB's native ``pg_catalog.pg_database`` exposes only (oid, datname);
    DBeaver expects the full Postgres column set or it errors on
    ``datallowconn``/``datistemplate``.  Our ``obsl_meta.pg_database``
    view provides sensible defaults so the probe succeeds.
    """

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT db.oid, db.* FROM pg_catalog.pg_database db "
        "WHERE 1=1 AND datallowconn AND NOT datistemplate"
    )
    column_names = [c.name for c in result.columns]
    assert "datname" in column_names
    assert "datallowconn" in column_names
    assert "datistemplate" in column_names
    assert result.row_count >= 1


def test_unhandled_probe_logs_warning(
    manager_with_model: SessionManager, caplog: pytest.LogCaptureFixture
) -> None:
    """A catalog query that throws emits PGWIRE_CATALOG_PROBE_UNHANDLED."""

    import logging

    import duckdb

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    caplog.set_level(logging.WARNING, logger="orionbelt.pgwire.catalog")
    with pytest.raises(duckdb.Error):
        emu.execute("SELECT * FROM pg_catalog.does_not_exist")
    assert any("PGWIRE_CATALOG_PROBE_UNHANDLED" in record.message for record in caplog.records)
