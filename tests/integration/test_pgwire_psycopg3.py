"""Live psycopg3 client tests against the pgwire surface (Step 5).

psycopg3 uses the extended-query protocol for any parameterised
``cursor.execute(sql, params)`` call — exactly the path BI tools and
ORMs drive in production. These tests boot a real :class:`PgWireServer`
on an ephemeral port, point a psycopg3 connection at it, and verify
that the round-trip survives prepared statements, parameter binding,
catalog probes, and the autocommit transaction wrappers psycopg3
issues by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

import psycopg
import pytest

from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult
from orionbelt.service.session_manager import SessionManager
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def pgwire_with_router_psycopg(monkeypatch: pytest.MonkeyPatch) -> tuple[PgWireServer, int]:
    """Spin up a pgwire server in a dedicated background event loop.

    psycopg3's sync API blocks the asyncio loop, so we can't reuse the
    asyncio-fixture pattern from the other integration tests. A
    background thread runs the server's event loop; the test thread
    drives psycopg3 against the bound port.
    """

    mgr = SessionManager()
    store = mgr.get_or_create_named("commerce")
    store.load_model(SAMPLE_MODEL_YAML)

    def fake_execute(sql: str, **_: Any) -> ExecutionResult:
        return ExecutionResult(
            columns=[
                ColumnMeta(name="Customer Country", type_hint="string"),
                ColumnMeta(name="Total Revenue", type_hint="number"),
            ],
            raw_rows=[["DE", 1234], ["US", 9876]],
            row_count=2,
        )

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    loop = asyncio.new_event_loop()
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        max_connections=8,
        query_handler=SemanticRouter(session_manager=mgr, default_dialect="duckdb").handle,
    )
    ready = threading.Event()

    async def _run() -> None:
        await server.start()
        ready.set()
        await server.serve_forever()

    def _loop_target() -> None:
        asyncio.set_event_loop(loop)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            loop.run_until_complete(_run())

    thread = threading.Thread(target=_loop_target, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    port = server.bound_port

    try:
        yield server, port
    finally:
        # Cancel the serve_forever task from inside the loop so
        # ``loop.run_until_complete`` returns cleanly.
        async def _stop() -> None:
            await server.stop()

        future = asyncio.run_coroutine_threadsafe(_stop(), loop)
        with contextlib.suppress(Exception):
            future.result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _dsn(port: int, database: str = "commerce") -> str:
    return f"host=127.0.0.1 port={port} user=obsl dbname={database}"


def test_psycopg3_select_one(pgwire_with_router_psycopg: tuple[PgWireServer, int]) -> None:
    """The canonical psycopg3 smoke: connect + run a literal SELECT."""

    _, port = pgwire_with_router_psycopg
    with psycopg.connect(_dsn(port), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        rows = cur.fetchall()
    assert rows == [(1,)]


def test_psycopg3_information_schema_columns(
    pgwire_with_router_psycopg: tuple[PgWireServer, int],
) -> None:
    """BI tools probe information_schema; psycopg3 must round-trip it.

    Also exercises psycopg3's automatic extended-query path: ``%s``
    placeholders force Parse / Bind / Describe / Execute / Sync.
    """

    _, port = pgwire_with_router_psycopg
    with psycopg.connect(_dsn(port), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            ("commerce", "model"),
        )
        names = [row[0] for row in cur.fetchall()]
    assert "Customer Country" in names
    assert "Total Revenue" in names


def test_psycopg3_pg_class_dt_style(
    pgwire_with_router_psycopg: tuple[PgWireServer, int],
) -> None:
    """psql ``\\dt``-shape query through psycopg3."""

    _, port = pgwire_with_router_psycopg
    with psycopg.connect(_dsn(port), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname, c.relkind "
            "FROM pg_catalog.pg_class c "
            "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r','p','') "
            "AND n.nspname NOT IN ('pg_catalog','information_schema') "
            "ORDER BY 1,2"
        )
        rows = [(row[0], row[1]) for row in cur.fetchall()]
    # Per-model schema layout: data table is ``<model>.model``.
    assert ("commerce", "model") in rows


def test_psycopg3_semantic_query_returns_rows(
    pgwire_with_router_psycopg: tuple[PgWireServer, int],
) -> None:
    """End-to-end: psycopg3 + SemanticRouter returns stub rows."""

    _, port = pgwire_with_router_psycopg
    with psycopg.connect(_dsn(port), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute('SELECT "Customer Country", "Total Revenue" FROM commerce')
        rows = cur.fetchall()
        description = cur.description
    assert rows == [("DE", 1234), ("US", 9876)]
    assert description is not None
    assert [d.name for d in description] == ["Customer Country", "Total Revenue"]
