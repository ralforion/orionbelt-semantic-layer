"""End-to-end cache-hit tests for shortcut /query/execute and oneshot batch.

Mocks the warehouse execution path (``execute_sql``) so we can verify the
cache wraps every execute-style endpoint, including the single-model
shortcut and the oneshot batch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import (
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.cache.file import FileCache
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML


def _stub_exec_result() -> ExecutionResult:
    """A small fixed ExecutionResult — independent of dialect/SQL."""
    return ExecutionResult(
        columns=[
            ColumnMeta("Customer Country", "string"),
            ColumnMeta("Total Revenue", "number", "#,##0.00"),
        ],
        raw_rows=[["US", 100.0], ["UK", 200.0]],
        row_count=2,
        execution_time_ms=1.0,
    )


@pytest.fixture
async def cached_client(tmp_path: Path):
    """App + client with FileCache backed by tmp_path and execute_sql mocked."""
    cache = FileCache(
        cache_dir=str(tmp_path),
        max_value_bytes=10 * 1024 * 1024,
        max_disk_bytes=50 * 1024 * 1024,
        max_ttl_seconds=3600,
        sweep_interval_seconds=3600,
    )
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
    )
    init_session_manager(
        mgr,
        query_execute_enabled=True,
        db_vendor="duckdb",
        cache=cache,
        cache_config=CacheRuntimeConfig(
            backend="file",
            min_ttl_seconds=1,
            max_ttl_seconds=3600,
            unknown_policy="default_ttl",
            unknown_default_ttl_seconds=300,
            heartbeat_auth_token="test-token",
        ),
    )

    transport = ASGITransport(app=app)
    with (
        patch("orionbelt.api.query_cache.execute_sql", return_value=_stub_exec_result()),
        patch("orionbelt.api.routers.oneshot.execute_sql", return_value=_stub_exec_result()),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, cache
    await cache.shutdown()
    reset_session_manager()


_QUERY_BODY = {
    "select": {
        "dimensions": ["Customer Country"],
        "measures": ["Total Revenue"],
    },
}


class TestSessionExecuteCache:
    async def test_session_execute_caches_then_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]

        first = await client.post(
            f"/v1/sessions/{sid}/query/execute",
            json={"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["cached"] is False

        second = await client.post(
            f"/v1/sessions/{sid}/query/execute",
            json={"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"},
        )
        assert second.status_code == 200
        body = second.json()
        assert body["cached"] is True
        assert body["cached_at"] is not None
        assert body["row_count"] == 2


class TestShortcutExecuteCache:
    async def test_shortcut_execute_caches_then_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        # Single-model-mode-style usage: one session, one model, then call shortcut.
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})

        first = await client.post(
            "/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"}
        )
        assert first.status_code == 200, first.text
        assert first.json()["cached"] is False

        second = await client.post(
            "/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"}
        )
        assert second.status_code == 200
        body = second.json()
        assert body["cached"] is True
        assert body["physical_tables"]


class TestOneshotBatchCache:
    async def test_oneshot_batch_caches_then_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client

        first = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "execute": True,
                "queries": [{"id": "q1", "query": _QUERY_BODY}],
                "dialect": "duckdb",
                "persist_model": True,
            },
        )
        assert first.status_code == 200, first.text
        first_data = first.json()
        sid = first_data["session_id"]
        mid = first_data["model_id"]
        assert first_data["results"][0]["cached"] is False

        second = await client.post(
            "/v1/oneshot/batch",
            json={
                "session_id": sid,
                "model_id": mid,
                "execute": True,
                "queries": [{"id": "q1", "query": _QUERY_BODY}],
                "dialect": "duckdb",
            },
        )
        assert second.status_code == 200
        result = second.json()["results"][0]
        assert result["cached"] is True
        assert result["row_count"] == 2


class TestShortcutPlanQuery:
    async def test_shortcut_plan_returns_plan(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})

        resp = await client.post(
            "/v1/query/plan",
            json={
                "model_id": "ignored-by-shortcut",
                "query": _QUERY_BODY,
                "dialect": "postgres",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["would_compile"] is True
        assert any("ORDERS" in t for t in data["physical_tables"])


_MODEL_WITH_EXAMPLES = (
    SAMPLE_MODEL_YAML
    + """
examples:
  - name: revenue_by_country
    description: Total completed-order revenue by customer country.
    intentTags: [revenue, geography]
    query:
      select:
        dimensions: [Customer Country]
        measures: [Total Revenue]
"""
)


class TestShortcutExamples:
    async def test_shortcut_list_examples(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples")
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()["examples"]]
        assert "revenue_by_country" in names

    async def test_shortcut_list_examples_intent_filter(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples", params={"intent": "revenue"})
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()["examples"]]
        assert names == ["revenue_by_country"]

    async def test_shortcut_get_example(self, cached_client: tuple[AsyncClient, FileCache]) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples/revenue_by_country")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "revenue_by_country"
        assert data["compiled_sql_preview"] is not None

    async def test_shortcut_get_example_404(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples/no_such_example")
        assert resp.status_code == 404


class TestHeartbeatInvalidatesShortcut:
    async def test_heartbeat_evicts_then_next_call_misses(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        # Prime the cache via the shortcut
        await client.post("/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"})
        hit = await client.post("/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"})
        assert hit.json()["cached"] is True

        # Heartbeat the underlying physical table → invalidate
        hb = await client.post(
            "/v1/heartbeat",
            json={"database": "WAREHOUSE", "schema": "PUBLIC", "table": "ORDERS"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert hb.status_code == 200
        assert hb.json()["invalidated_cache_entries"] >= 1

        # Next call must be a fresh miss
        miss = await client.post(
            "/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"}
        )
        assert miss.json()["cached"] is False
