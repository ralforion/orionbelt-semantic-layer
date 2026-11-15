"""A query counts under the surface it came in through, across threads.

REST runs a query in an executor thread and pgwire on its connection's task;
the surface each sets (``metrics.SURFACE``) has to reach ``execute_sql``
there. These go through the real REST app and a real pgwire listener, with
``execute_sql`` replaced by one that records the surface it sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("prometheus_client")
psycopg = pytest.importorskip("psycopg")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.api.deps import init_session_manager, reset_session_manager  # noqa: E402
from orionbelt.pgwire.router import SemanticRouter  # noqa: E402
from orionbelt.pgwire.server import PgWireServer  # noqa: E402
from orionbelt.service import metrics  # noqa: E402
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402
from tests.conftest import SAMPLE_MODEL_YAML  # noqa: E402


@pytest.fixture
def metrics_on() -> Iterator[None]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    metrics.start(enabled=True, host="127.0.0.1", port=port)
    try:
        yield
    finally:
        metrics.stop()


async def test_a_rest_query_counts_as_rest_in_its_executor_thread(metrics_on: None) -> None:
    seen: list[tuple[str, str]] = []

    def recording_execute_sql(sql: str, **_: Any) -> ExecutionResult:
        seen.append((metrics.SURFACE.get(), threading.current_thread().name))
        return ExecutionResult(
            columns=[ColumnMeta(name="Customer Country", type_hint="string")],
            raw_rows=[["US"]],
            row_count=1,
        )

    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    init_session_manager(mgr, query_execute_enabled=True, db_vendor="duckdb")
    try:
        with patch("orionbelt.api.query_cache.execute_sql", recording_execute_sql):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                sid = (await client.post("/v1/sessions")).json()["session_id"]
                mid = (
                    await client.post(
                        f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
                    )
                ).json()["model_id"]
                response = await client.post(
                    f"/v1/sessions/{sid}/query/execute",
                    json={
                        "model_id": mid,
                        "query": {"select": {"dimensions": ["Customer Country"]}},
                        "dialect": "duckdb",
                    },
                )
        assert response.status_code == 200, response.text
    finally:
        reset_session_manager()
        mgr.stop()
    assert seen and all(surface == "rest" for surface, _ in seen), seen
    # It ran in an executor thread, not on the event loop's.
    assert all(name != threading.main_thread().name for _, name in seen), seen


@pytest.fixture
def pgwire_port(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, list[str]]]:
    seen: list[str] = []

    def recording_execute_sql(sql: str, **_: Any) -> ExecutionResult:
        seen.append(metrics.SURFACE.get())
        return ExecutionResult(
            columns=[
                ColumnMeta(name="Customer Country", type_hint="string"),
                ColumnMeta(name="Total Revenue", type_hint="number"),
            ],
            raw_rows=[["DE", 1]],
            row_count=1,
        )

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", recording_execute_sql)
    mgr = SessionManager()
    mgr.get_or_create_named("commerce").load_model(SAMPLE_MODEL_YAML)
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        max_connections=4,
        query_handler=SemanticRouter(session_manager=mgr, default_dialect="duckdb").handle,
    )
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def serve() -> None:
        await server.start()
        ready.set()
        await server.serve_forever()

    def run() -> None:
        asyncio.set_event_loop(loop)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            loop.run_until_complete(serve())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        yield server.bound_port, seen
    finally:
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        mgr.stop()


def test_a_pgwire_query_counts_as_pgwire(
    metrics_on: None, pgwire_port: tuple[int, list[str]]
) -> None:
    port, seen = pgwire_port
    dsn = f"host=127.0.0.1 port={port} user=obsl dbname=commerce"
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute('SELECT "Customer Country", "Total Revenue" FROM commerce')
        assert cur.fetchall() == [("DE", 1)]
    assert seen == ["pgwire"]
