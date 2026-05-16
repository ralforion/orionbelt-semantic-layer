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


def test_refresh_creates_one_schema_per_model(manager_with_model: SessionManager) -> None:
    """Each loaded model gets its own DuckDB schema named after the model."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
    schemas = [row[0] for row in result.rows]
    assert "commerce" in schemas


def test_data_table_is_named_model(manager_with_model: SessionManager) -> None:
    """The data table inside each model schema is called ``model``."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='commerce' AND table_type='BASE TABLE'"
    )
    assert [row[0] for row in result.rows] == ["model"]


def test_table_has_expected_columns(manager_with_model: SessionManager) -> None:
    """All model dimensions, measures, and metrics are exposed as columns."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='commerce' AND table_name='model' "
        "ORDER BY ordinal_position"
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
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='commerce' AND table_name='model'"
    )
    assert result.rows[0][0] == 1


def test_refresh_drops_stale_models() -> None:
    """A model removed from the SessionManager disappears from the catalog."""

    mgr = SessionManager()
    store = mgr.get_or_create_named("temp_model")
    store.load_model(SAMPLE_MODEL_YAML)
    emu = CatalogEmulator()
    emu.refresh(mgr)
    empty = SessionManager()
    emu.refresh(empty)
    result = emu.execute(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name='temp_model'"
    )
    assert result.rows[0][0] == 0


def test_psql_dt_style_query_returns_rows(manager_with_model: SessionManager) -> None:
    """\\dt's actual SQL must surface the model table under its own schema."""

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


def test_empty_session_manager_yields_no_model_schemas() -> None:
    emu = CatalogEmulator()
    emu.refresh(SessionManager())
    result = emu.execute(
        "SELECT count(*) FROM information_schema.schemata "
        "WHERE schema_name NOT IN ('main','obsl_meta','pg_catalog','information_schema')"
    )
    assert result.rows[0][0] == 0


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
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relname='model' AND n.nspname='commerce'"
    )
    assert result.row_count == 1
    row = result.rows[0]
    assert row[0] == "model"
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
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname='model' AND n.nspname='commerce' "
        "ORDER BY a.attnum"
    )
    types = {row[0]: row[1] for row in result.rows}
    assert types.get("Customer Country") == 1043
    assert types.get("Total Revenue") == 701


def test_refresh_preserves_data_table_oid_across_calls(
    manager_with_model: SessionManager,
) -> None:
    """Stable refresh: same model → same oid (psql \\d's two-step probe)."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    sql = (
        "SELECT c.oid FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relname='model' AND n.nspname='commerce'"
    )
    oid_before = emu.execute(sql).rows[0][0]
    emu.refresh(manager_with_model)
    oid_after = emu.execute(sql).rows[0][0]
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


def test_pg_database_returns_single_orionbelt_row(
    manager_with_model: SessionManager,
) -> None:
    """pg_database surfaces the OBSL brand, not DuckDB catalog names.

    Every loaded model is exposed as a TABLE under this single database.
    BI-tool trees get a clean top-level "orionbelt" node and the model
    names appear in the Tables list — not as sibling databases.
    """

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute("SELECT datname FROM pg_catalog.pg_database")
    names = [row[0] for row in result.rows]
    assert names == ["orionbelt"]
    # DuckDB's defaults must not leak through.
    assert "memory" not in names
    assert "system" not in names


def test_pg_namespace_hides_obsl_meta(
    manager_with_model: SessionManager,
) -> None:
    """Internal obsl_meta schema is filtered from pg_namespace listings."""

    emu = CatalogEmulator()
    emu.refresh(manager_with_model)
    result = emu.execute("SELECT nspname FROM pg_catalog.pg_namespace")
    names = [row[0] for row in result.rows]
    assert "obsl_meta" not in names
    assert "pg_catalog" not in names
    assert "information_schema" not in names


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
