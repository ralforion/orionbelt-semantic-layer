"""Integration tests for REST API-key auth (design/PLAN_authentication.md Phase 1)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import CacheRuntimeConfig, init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

TEST_KEY = "obsl_pat_test_key_0123456789abcdef"
HEARTBEAT_TOKEN = "hb-secret-token-0123456789"


def _make_manager(settings: Settings) -> SessionManager:
    return SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        max_age_seconds=settings.session_max_age_seconds,
        max_sessions=settings.max_sessions,
        max_models_per_session=settings.max_models_per_session,
        cleanup_interval=settings.session_cleanup_interval,
    )


@pytest.fixture
def auth_app():
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        auth_mode="api_key",
        api_keys=TEST_KEY,
    )
    app = create_app(settings=settings)
    init_session_manager(
        _make_manager(settings),
        auth_mode="api_key",
        api_keys=TEST_KEY,
    )
    yield app
    reset_session_manager()


@pytest.fixture
async def auth_client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAuthDisabledByDefault:
    """The default (no auth) path must be unchanged."""

    async def test_v1_open_without_key(self) -> None:
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        init_session_manager(_make_manager(settings))
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/v1/dialects")
                assert resp.status_code == 200
                health = await c.get("/health")
                assert health.json()["auth_mode"] == "none"
        finally:
            reset_session_manager()


class TestAuthEnabled:
    async def test_health_exempt_and_reports_mode(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["auth_mode"] == "api_key"

    async def test_robots_exempt(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/robots.txt")
        assert resp.status_code == 200

    async def test_missing_key_401(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/v1/dialects")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers
        assert resp.json()["detail"]["code"] == "AUTH_REQUIRED"

    async def test_wrong_key_403(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/v1/dialects", headers={"X-API-Key": "nope-wrong-key-123456"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "AUTH_INVALID"

    async def test_valid_key_header(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/v1/dialects", headers={"X-API-Key": TEST_KEY})
        assert resp.status_code == 200

    async def test_valid_key_bearer_fallback(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get(
            "/v1/dialects", headers={"Authorization": f"Bearer {TEST_KEY}"}
        )
        assert resp.status_code == 200

    async def test_protected_post_blocked_without_key(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.post("/v1/sessions", json={})
        assert resp.status_code == 401


class TestStartupValidation:
    async def test_empty_keys_fails_fast(self) -> None:
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        with pytest.raises(Exception, match="API_KEYS is empty"):
            init_session_manager(_make_manager(settings), auth_mode="api_key", api_keys="")
        reset_session_manager()


class TestHeartbeatExemptFromGlobalAuth:
    """Heartbeat keeps its own Bearer-token auth and is not gated by API-key auth.

    The global API auth treats Authorization: Bearer as an API-key fallback, so
    routing heartbeat through it would reject the heartbeat token as an invalid
    API key (403). Heartbeat is included outside the auth-bearing router.
    """

    @pytest.fixture
    async def hb_client(self):
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        init_session_manager(
            _make_manager(settings),
            auth_mode="api_key",
            api_keys=TEST_KEY,
            cache_config=CacheRuntimeConfig(heartbeat_auth_token=HEARTBEAT_TOKEN),
        )
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            reset_session_manager()

    async def test_heartbeat_token_reaches_handler(self, hb_client: AsyncClient) -> None:
        resp = await hb_client.post(
            "/v1/heartbeat",
            headers={"Authorization": f"Bearer {HEARTBEAT_TOKEN}"},
            json={"database": "db", "schema": "public", "table": "orders"},
        )
        # Not blocked by global API-key auth (would be 403 AUTH_INVALID); the
        # heartbeat handler runs and accepts its own token.
        assert resp.status_code == 200

    async def test_heartbeat_uses_own_auth_not_global_key(self, hb_client: AsyncClient) -> None:
        # Sending the global API key but no heartbeat Bearer hits the heartbeat
        # handler's own check (401 missing Authorization), not global auth.
        resp = await hb_client.post(
            "/v1/heartbeat",
            headers={"X-API-Key": TEST_KEY},
            json={"database": "db", "schema": "public", "table": "orders"},
        )
        assert resp.status_code == 401
