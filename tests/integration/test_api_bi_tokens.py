"""BI tokens over REST: create, use, list and revoke (``/v1/auth/tokens``)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import reset_auth
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML
from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

SERVICE_KEY = "obsl_pat_bi_token_tests_service_key_0123456789"


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, FakeIssuer]]:
    idp = FakeIssuer()
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
        token_store_path=str(tmp_path / "tokens" / "bi.sqlite"),
        token_max_days=180,
    )
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc=oidc_config(settings),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, idp
    reset_session_manager()
    reset_auth()


def _as(idp: FakeIssuer, sub: str, scope: str = "obsl:write") -> dict[str, str]:
    return {"Authorization": f"Bearer {idp.token(sub, scope=scope)}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create(client: AsyncClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/tokens", json={"label": "Tableau", **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_a_signed_in_user_creates_one_and_a_bi_tool_uses_it(api: Any) -> None:
    client, idp = api
    created = await _create(client, _as(idp, "alice"))
    assert created["token"].startswith(f"obsl_tok_{created['id']}_")
    assert created["scopes"] == ["obsl:read"]  # the default
    assert created["owner"] == "alice"
    # Read works; writing needs a scope the token was not given.
    session = await client.post("/v1/sessions", headers=_bearer(created["token"]))
    assert session.status_code == 201, session.text
    sid = session.json()["session_id"]
    load = await client.post(
        f"/v1/sessions/{sid}/models",
        json={"model_yaml": SAMPLE_MODEL_YAML},
        headers=_bearer(created["token"]),
    )
    assert load.status_code == 403
    # The session is alice's: her sign-in sees it, bob's does not.
    assert (await client.get(f"/v1/sessions/{sid}", headers=_as(idp, "alice"))).status_code == 200
    assert (await client.get(f"/v1/sessions/{sid}", headers=_as(idp, "bob"))).status_code == 404


async def test_scopes_are_a_subset_of_the_creator_s(api: Any) -> None:
    client, idp = api
    created = await _create(client, _as(idp, "alice"), scopes=["obsl:write"])
    assert created["scopes"] == ["obsl:read", "obsl:write"]  # write implies read
    reader = _as(idp, "carol", scope="obsl:read")
    refused = await client.post(
        "/v1/auth/tokens", json={"label": "x", "scopes": ["obsl:write"]}, headers=reader
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "SCOPE_NOT_HELD"
    unknown = await client.post(
        "/v1/auth/tokens", json={"label": "x", "scopes": ["obsl:root"]}, headers=reader
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "UNKNOWN_SCOPE"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"label": "x", "days": 181}, 422),  # TOKEN_MAX_DAYS is 180 here
        ({"label": "x", "days": 0}, 422),
        ({"label": ""}, 422),
        ({"label": "x", "scope": ["obsl:write"]}, 422),  # a misspelt field is refused
    ],
)
async def test_what_a_request_may_ask_for(api: Any, body: dict[str, Any], expected: int) -> None:
    client, idp = api
    response = await client.post("/v1/auth/tokens", json=body, headers=_as(idp, "alice"))
    assert response.status_code == expected, response.text


async def test_only_a_signed_in_user_creates_one(api: Any) -> None:
    client, idp = api
    by_key = await client.post(
        "/v1/auth/tokens", json={"label": "x"}, headers={"X-API-Key": SERVICE_KEY}
    )
    assert by_key.status_code == 403
    assert by_key.json()["detail"]["code"] == "BI_TOKEN_NEEDS_SIGN_IN"
    token = (await _create(client, _as(idp, "alice")))["token"]
    by_token = await client.post("/v1/auth/tokens", json={"label": "x"}, headers=_bearer(token))
    assert by_token.status_code == 403


async def test_listing_shows_your_own_and_an_admin_everyone_s(api: Any) -> None:
    client, idp = api
    alices = await _create(client, _as(idp, "alice"))
    bobs = await _create(client, _as(idp, "bob"))
    mine = (await client.get("/v1/auth/tokens", headers=_as(idp, "alice"))).json()["tokens"]
    assert [t["id"] for t in mine] == [alices["id"]]
    assert "token" not in mine[0]  # the secret is shown once only
    everyone = "/v1/auth/tokens?all=true"
    assert (await client.get(everyone, headers=_as(idp, "alice"))).status_code == 403
    admin = _as(idp, "root", scope="obsl:admin")
    listed = (await client.get(everyone, headers=admin)).json()["tokens"]
    assert {t["id"] for t in listed} == {alices["id"], bobs["id"]}


async def test_revoking(api: Any) -> None:
    client, idp = api
    alices = await _create(client, _as(idp, "alice"))
    bobs = await _create(client, _as(idp, "bob"))
    # Not bob's to revoke; alice's own revoke works at once.
    revoke_alices = f"/v1/auth/tokens/{alices['id']}"
    assert (await client.delete(revoke_alices, headers=_as(idp, "bob"))).status_code == 404
    assert (await client.delete(revoke_alices, headers=_as(idp, "alice"))).status_code == 204
    refused = await client.post("/v1/sessions", headers=_bearer(alices["token"]))
    assert refused.status_code == 401
    # An admin offboards bob: every token of his, at once.
    admin = _as(idp, "root", scope="obsl:admin")
    offboard = "/v1/auth/tokens?owner=bob"
    assert (await client.delete(offboard, headers=_as(idp, "alice"))).status_code == 403
    assert (await client.delete(offboard, headers=admin)).json() == {"revoked": 1}
    assert (await client.post("/v1/sessions", headers=_bearer(bobs["token"]))).status_code == 401


async def test_not_there_without_oidc(tmp_path: Path) -> None:
    app = create_app(settings=Settings())
    init_session_manager(SessionManager(ttl_seconds=3600, cleanup_interval=9999))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.post("/v1/auth/tokens", json={"label": "x"})
        assert response.status_code == 404
    finally:
        reset_session_manager()
        reset_auth()
