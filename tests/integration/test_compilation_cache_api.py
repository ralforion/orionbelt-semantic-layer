"""The compilation cache behind the REST API: hits, stats and a separate clear."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import CacheRuntimeConfig, init_session_manager, reset_session_manager
from orionbelt.cache.noop import NoopCache
from orionbelt.service.compilation_cache import CompilationCache
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML

_QUERY = {"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}}


def _app(cache: CompilationCache):  # noqa: ANN202 - FastAPI app
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
        compilation_cache=cache,
    )
    init_session_manager(mgr, cache=NoopCache(), cache_config=CacheRuntimeConfig(backend="noop"))
    return app


@pytest.fixture
async def client():
    transport = ASGITransport(app=_app(CompilationCache(max_entries=100)))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    reset_session_manager()


async def _compile(client: AsyncClient) -> dict:
    sid = (await client.post("/v1/sessions")).json()["session_id"]
    load = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
    mid = load.json()["model_id"]
    r = await client.post(
        f"/v1/sessions/{sid}/query/sql",
        json={"model_id": mid, "query": _QUERY, "dialect": "postgres"},
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestCompilationCacheApi:
    async def test_repeated_query_is_a_hit_across_sessions(self, client: AsyncClient) -> None:
        first = await _compile(client)
        second = await _compile(client)
        assert second["sql"] == first["sql"]
        stats = (await client.get("/v1/cache/compilation")).json()
        assert stats["enabled"] is True
        assert (stats["misses"], stats["hits"], stats["entries"]) == (1, 1, 1)

    async def test_clear_drops_compiled_queries_only(self, client: AsyncClient) -> None:
        await _compile(client)
        r = await client.post("/v1/cache/compilation/clear")
        assert r.json() == {"entries_cleared": 1}
        assert (await client.get("/v1/cache/compilation")).json()["entries"] == 0
        assert (await client.get("/v1/cache/stats")).status_code == 200

    async def test_zero_entries_disables_it(self) -> None:
        transport = ASGITransport(app=_app(CompilationCache()))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await _compile(c)
            await _compile(c)
            stats = (await c.get("/v1/cache/compilation")).json()
        reset_session_manager()
        assert stats["enabled"] is False
        assert (stats["hits"], stats["entries"]) == (0, 0)


def test_settings_enable_the_cache_by_default() -> None:
    settings = Settings()
    assert settings.compile_cache_max_entries == 2048
    cache = CompilationCache(
        max_entries=settings.compile_cache_max_entries,
        max_bytes=settings.compile_cache_max_bytes,
        max_entry_bytes=settings.compile_cache_max_entry_bytes,
    )
    assert cache.enabled
