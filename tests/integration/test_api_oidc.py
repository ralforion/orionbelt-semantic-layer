"""REST under AUTH_MODE=oidc, end to end with signed tokens (no IdP needed).

The issuer's keys come from OIDC_JWKS_FILE, the air-gapped setting, so
nothing is fetched. Everything else is the code a deployment runs.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML
from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

SERVICE_KEY = "obsl_pat_oidc_service_key_0123456789abcdef"


@pytest.fixture
def idp() -> FakeIssuer:
    return FakeIssuer()


@pytest.fixture
def app(idp: FakeIssuer, tmp_path: Path) -> Iterator[FastAPI]:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(idp.jwks()))
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_file=str(jwks),
    )
    application = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode=settings.auth_mode,
        api_keys=settings.api_keys,
        oidc=oidc_config(settings),
    )
    yield application
    reset_session_manager()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAuthentication:
    async def test_health_reports_oidc(self, client: AsyncClient) -> None:
        assert (await client.get("/health")).json()["auth_mode"] == "oidc"

    async def test_valid_token(self, client: AsyncClient, idp: FakeIssuer) -> None:
        resp = await client.get("/v1/dialects", headers=_bearer(idp.token()))
        assert resp.status_code == 200

    async def test_no_credential_is_challenged_for_a_bearer_token(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/v1/dialects")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Bearer realm="obsl"'

    async def test_expired_token_is_invalid_token(
        self, client: AsyncClient, idp: FakeIssuer
    ) -> None:
        resp = await client.get("/v1/dialects", headers=_bearer(idp.token(expires_in=-300)))
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == (
            'Bearer realm="obsl", error="invalid_token", error_description="Token has expired"'
        )
        assert resp.json()["detail"] == {"code": "AUTH_INVALID", "message": "Token has expired"}

    async def test_service_key_next_to_tokens(self, client: AsyncClient) -> None:
        created = await client.post("/v1/sessions", headers={"X-API-Key": SERVICE_KEY})
        assert created.status_code == 201
        sid = created.json()["session_id"]
        loaded = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
            headers=_bearer(SERVICE_KEY),
        )
        assert loaded.status_code == 201

    async def test_wrong_service_key_stays_403(self, client: AsyncClient) -> None:
        resp = await client.get("/v1/dialects", headers={"X-API-Key": SERVICE_KEY[:-1] + "X"})
        assert resp.status_code == 403


class TestScopes:
    async def test_reader_cannot_load_a_model(self, client: AsyncClient, idp: FakeIssuer) -> None:
        token = idp.token("reader", scope="obsl:read")
        sid = (await client.post("/v1/sessions", headers=_bearer(token))).json()["session_id"]
        resp = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
            headers=_bearer(token),
        )
        assert resp.status_code == 403
        assert resp.headers["www-authenticate"] == (
            'Bearer realm="obsl", error="insufficient_scope", scope="obsl:write"'
        )

    async def test_token_without_obsl_scopes_reads_nothing(
        self, client: AsyncClient, idp: FakeIssuer
    ) -> None:
        resp = await client.get("/v1/dialects", headers=_bearer(idp.token(scope="openid")))
        assert resp.status_code == 403

    async def test_admin_reaches_the_cache(self, client: AsyncClient, idp: FakeIssuer) -> None:
        writer = _bearer(idp.token(scope="obsl:write"))
        admin = _bearer(idp.token(scope="obsl:admin"))
        assert (await client.post("/v1/cache/clear", headers=writer)).status_code == 403
        assert (await client.post("/v1/cache/clear", headers=admin)).status_code == 200


class TestSessionOwnership:
    async def test_users_do_not_see_each_others_sessions(
        self, client: AsyncClient, idp: FakeIssuer
    ) -> None:
        alice = _bearer(idp.token("alice", scope="obsl:write"))
        bob = _bearer(idp.token("bob", scope="obsl:write"))
        sid = (await client.post("/v1/sessions", headers=alice)).json()["session_id"]
        loaded = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}, headers=alice
        )
        assert loaded.status_code == 201

        assert (await client.get(f"/v1/sessions/{sid}", headers=bob)).status_code == 404
        assert (await client.get("/v1/dimensions", headers=bob)).status_code == 404
        listed = (await client.get("/v1/sessions", headers=bob)).json()["sessions"]
        assert listed == []
        assert (await client.get("/v1/dimensions", headers=alice)).status_code == 200

    async def test_service_key_sees_users_sessions(
        self, client: AsyncClient, idp: FakeIssuer
    ) -> None:
        alice = _bearer(idp.token("alice", scope="obsl:write"))
        sid = (await client.post("/v1/sessions", headers=alice)).json()["session_id"]
        resp = await client.get(f"/v1/sessions/{sid}", headers={"X-API-Key": SERVICE_KEY})
        assert resp.status_code == 200


async def test_log_lines_carry_the_callers_sub(
    client: AsyncClient, idp: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    original = SessionManager.create_session

    def spy(self: SessionManager, metadata: dict[str, str] | None = None) -> Any:
        seen.append(structlog.contextvars.get_contextvars())
        return original(self, metadata)

    monkeypatch.setattr(SessionManager, "create_session", spy)
    await client.post("/v1/sessions", headers=_bearer(idp.token("alice")))
    await client.post("/v1/sessions", headers={"X-API-Key": SERVICE_KEY})
    assert seen[0].get("sub") == "alice"
    assert "sub" not in seen[1]
