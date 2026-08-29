"""Integration tests: compile + execute a semantic model on live MotherDuck.

MotherDuck is DuckDB served remotely, so it needs **no new dialect** — the
existing ``duckdb`` codegen is what runs. What is new is the connection path:
a ``md:`` database string carrying an authentication token. These tests cover
that path end to end, and use the **local** ``examples/orionbelt_1_commerce.duckdb``
as the source of truth: any disagreement is a MotherDuck-vs-DuckDB behaviour
difference, which is exactly what we want to catch early.

Opt-in — requires a live account::

    uv run pytest -m motherduck

Required env vars (skipped if missing):

    DUCKDB_DATABASE           ``md:<database>`` — the MotherDuck database
    MOTHERDUCK_ACCESS_TOKEN   access token (or the lowercase ``motherduck_token``
                              that MotherDuck's own CLI exports)

Seeding is out of band, like the other cloud warehouses — the data is static
and re-loading it per session would dominate the suite::

    uv run python scripts/seed_cloud_vendor.py motherduck
    uv run python scripts/seed_cloud_vendor.py --check motherduck

That reuses the generated ``duckdb`` seed dump, because MotherDuck accepts the
same SQL. **It drops and recreates the ``orionbelt_1`` schema**, so point it
only at a database you own. Tests skip (rather than fail) when the schema is
absent, so an unseeded account does not look like a regression.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.motherduck

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_COMMERCE = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"
SCHEMA = "orionbelt_1"


def _token_env_names() -> tuple[str, ...]:
    """Every env var the router accepts for the MotherDuck token.

    Read from ``_ENV_ALIASES`` rather than restated here. A local copy of the
    list is exactly what went stale when ``MOTHERDUCK_TOKEN`` was added to the
    router: the live fixture skipped for anyone using that spelling, and
    ``test_missing_token_fails_fast`` failed because it never cleared it.
    """
    from ob_flight.db_router import _ENV_ALIASES

    canonical = "MOTHERDUCK_ACCESS_TOKEN"
    return (canonical, *_ENV_ALIASES.get(canonical, ()))


@pytest.fixture(scope="module")
def motherduck_env() -> dict[str, str]:
    """Skip unless the router considers MotherDuck configured.

    "Configured" is asked of the router rather than re-derived here. The
    token may arrive through any of three env vars *or* embedded directly in
    ``DUCKDB_DATABASE`` as ``motherduck_token=`` / ``read_scaling_token=``;
    a local rule that only looked at the env vars skipped the whole live
    suite for the documented embedded forms even though the router accepted
    them.
    """
    from ob_flight.db_router import MotherDuckTokenMissingError, get_credentials

    database = os.environ.get("DUCKDB_DATABASE", "")
    if not database.startswith("md:"):
        pytest.skip("DUCKDB_DATABASE is not a MotherDuck database (md:<name>)")
    try:
        creds = get_credentials("duckdb")
    except MotherDuckTokenMissingError as exc:
        pytest.skip(str(exc))
    return {"database": str(creds["database"])}


@pytest.fixture(scope="module")
def seeded(motherduck_env: dict[str, str]) -> None:
    """Skip unless the commerce seed is present on the remote database."""
    from orionbelt.service.db_executor import execute_sql

    try:
        result = execute_sql(f'SELECT COUNT(*) AS n FROM "{SCHEMA}"."sales"', dialect="duckdb")
    except Exception as exc:  # noqa: BLE001 — any failure means "not seeded"
        pytest.skip(
            f"{SCHEMA} not loaded on MotherDuck ({str(exc)[:120]}). "
            "Run: uv run python scripts/seed_cloud_vendor.py motherduck"
        )
    if not result.rows or not result.rows[0][0]:
        pytest.skip(f"{SCHEMA}.sales is empty on MotherDuck; run the seeder")


# ``examples/orionbelt_1_commerce.duckdb`` stores salesamount / salesquantity as
# DOUBLE, while the generated seed dump narrows them to DECIMAL(18,2) — so the
# remote tables are decimal and the local example is float. Summing the two is
# exact-decimal arithmetic on one side and float accumulation on the other
# (8305358.25 vs 8305358.249999998), which never matches byte-for-byte and is
# not a MotherDuck difference. The local side casts to the dump's declared type
# so both sides compute the same thing.
_SEEDED_DECIMAL = "DECIMAL(18, 2)"


def _local(sql: str) -> list[tuple[Any, ...]]:
    """Run the same SQL against the local commerce DuckDB — the truth side."""
    import duckdb

    if not LOCAL_COMMERCE.exists():
        pytest.skip(f"local truth database missing: {LOCAL_COMMERCE}")
    conn = duckdb.connect(str(LOCAL_COMMERCE), read_only=True)
    try:
        return list(conn.execute(sql).fetchall())
    finally:
        conn.close()


class TestConnection:
    """The connection path — the only genuinely new part for MotherDuck."""

    def test_token_is_folded_into_the_database_string(self, motherduck_env: dict[str, str]) -> None:
        """``duckdb.connect`` has no token argument, so it rides on the URI."""
        from ob_flight.db_router import _MOTHERDUCK_URI_TOKEN_PARAMS, get_credentials

        database = get_credentials("duckdb")["database"]
        assert database.startswith("md:")
        # Either accepted parameter counts: a read-scaling token is spelled
        # differently and is no less a token. Taken from the router so the
        # assertion cannot narrow behind it.
        assert any(f"{p}=" in database for p in _MOTHERDUCK_URI_TOKEN_PARAMS), database

    def test_missing_token_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tokenless ``md:`` must raise, never reach interactive auth.

        Without a token the DuckDB extension falls back to browser-based
        authentication, which on a server does not error — it hangs. This is
        the guard that turns that into a configuration error.
        """
        from ob_flight.db_router import MotherDuckTokenMissingError, get_credentials

        monkeypatch.setenv("DUCKDB_DATABASE", "md:some_db")
        for name in _token_env_names():
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(MotherDuckTokenMissingError, match="MOTHERDUCK_ACCESS_TOKEN"):
            get_credentials("duckdb")

    def test_round_trip_preserves_types(self, motherduck_env: dict[str, str]) -> None:
        from orionbelt.service.db_executor import execute_sql

        result = execute_sql(
            "SELECT 1 AS n, 2.50::DECIMAL(18,2) AS d, DATE '2026-01-02' AS dt, 'x' AS s",
            dialect="duckdb",
        )
        assert [c.type_hint for c in result.columns] == ["number", "number", "datetime", "string"]
        assert result.rows == [[1, Decimal("2.50"), "2026-01-02", "x"]]


class TestAgainstLocalTruth:
    """Remote results must match the same query run locally."""

    def test_row_count_matches(self, seeded: None) -> None:
        from orionbelt.service.db_executor import execute_sql

        sql = f'SELECT COUNT(*) AS n FROM "{SCHEMA}"."sales"'
        remote = execute_sql(sql, dialect="duckdb").rows[0][0]
        assert remote == _local(sql)[0][0]

    def test_grouped_aggregate_matches(self, seeded: None) -> None:
        """A shape the semantic layer actually emits: GROUP BY + SUM over DECIMAL."""
        from orionbelt.service.db_executor import execute_sql

        remote_sql = (
            'SELECT "salespaymenttype" AS pt, SUM("salesamount") AS amt '
            f'FROM "{SCHEMA}"."sales" GROUP BY 1 ORDER BY 1'
        )
        local_sql = (
            f'SELECT "salespaymenttype" AS pt, SUM("salesamount"::{_SEEDED_DECIMAL}) AS amt '
            f'FROM "{SCHEMA}"."sales" GROUP BY 1 ORDER BY 1'
        )
        remote = [tuple(r) for r in execute_sql(remote_sql, dialect="duckdb").rows]
        local = [tuple(r) for r in _local(local_sql)]
        assert remote == local, f"remote={remote[:3]} local={local[:3]}"


class TestSemanticQuery:
    """The full path: OBML -> compiled DuckDB SQL -> executed on MotherDuck."""

    MODEL = f"""
version: 1.0
dataObjects:
  Sales:
    code: sales
    database: md
    schema: {SCHEMA}
    columns:
      Sales ID:      {{code: salesid, abstractType: string, primaryKey: true}}
      Payment Type:  {{code: salespaymenttype, abstractType: string}}
      Sales Amount:  {{code: salesamount, abstractType: float}}
dimensions:
  Payment Type:
    dataObject: Sales
    column: Payment Type
    resultType: string
measures:
  Total Sales:
    columns:
      - {{dataObject: Sales, column: Sales Amount}}
    resultType: float
    aggregation: sum
"""

    def test_compiled_semantic_query_matches_local(self, seeded: None) -> None:
        from orionbelt.compiler.pipeline import CompilationPipeline
        from orionbelt.models.query import QueryObject, QuerySelect
        from orionbelt.service.db_executor import execute_sql
        from orionbelt.service.model_store import ModelStore

        store = ModelStore()
        loaded = store.load_model(self.MODEL, dedup=False)
        model = store.get_model(loaded.model_id)

        query = QueryObject(
            select=QuerySelect(dimensions=["Payment Type"], measures=["Total Sales"])
        )
        compiled = CompilationPipeline().compile(query, model, "duckdb")

        # The model qualifies as md.orionbelt_1.sales; the local truth database
        # has no "md" catalog, so compare against the same shape unqualified.
        remote = sorted(tuple(r) for r in execute_sql(compiled.sql, dialect="duckdb").rows)
        local = sorted(
            _local(
                f'SELECT "salespaymenttype", SUM("salesamount"::{_SEEDED_DECIMAL}) '
                f'FROM "{SCHEMA}"."sales" GROUP BY 1'
            )
        )
        assert remote == local, f"remote={remote[:3]} local={local[:3]}"
