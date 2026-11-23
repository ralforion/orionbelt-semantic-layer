"""REST scopes and session ownership with distinct users (PLAN_authentication.md 4a).

OIDC is not wired yet (Phase 4b), so ``orionbelt.auth.authenticate`` is
replaced by a lookup from a test credential to a user principal. Everything
after it, the scope check, the session owner, and the not-found answer, is
the real code path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orionbelt import auth as auth_core
from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    AuthInvalidError,
    AuthRequiredError,
    Principal,
    current_principal,
)
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML

_PRINCIPALS = {
    "alice-token": Principal(kind="user", sub="alice", scopes=frozenset({SCOPE_READ, SCOPE_WRITE})),
    "bob-token": Principal(kind="user", sub="bob", scopes=frozenset({SCOPE_READ, SCOPE_WRITE})),
    "reader-token": Principal(kind="user", sub="reader", scopes=frozenset({SCOPE_READ})),
    "admin-token": Principal(
        kind="user", sub="root", scopes=frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN})
    ),
}


def _fake_authenticate(credential: str | None) -> Principal:
    if not credential:
        raise AuthRequiredError("Bearer token required")
    try:
        return _PRINCIPALS[credential]
    except KeyError:
        raise AuthInvalidError("Invalid token") from None


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(auth_core, "authenticate", _fake_authenticate)
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    application = create_app(settings=settings)
    init_session_manager(SessionManager(ttl_seconds=3600, cleanup_interval=9999))
    yield application
    reset_session_manager()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _session_with_model(client: AsyncClient, token: str) -> tuple[str, str]:
    created = await client.post("/v1/sessions", headers=_as(token))
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    loaded = await client.post(
        f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}, headers=_as(token)
    )
    assert loaded.status_code == 201, loaded.text
    return sid, loaded.json()["model_id"]


class TestScopes:
    async def test_reader_can_start_a_session_but_not_load_a_model(
        self, client: AsyncClient
    ) -> None:
        created = await client.post("/v1/sessions", headers=_as("reader-token"))
        assert created.status_code == 201
        sid = created.json()["session_id"]
        loaded = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
            headers=_as("reader-token"),
        )
        assert loaded.status_code == 403
        assert loaded.json()["detail"] == {
            "code": "AUTH_INSUFFICIENT_SCOPE",
            "message": "Insufficient scope: requires obsl:write",
        }

    async def test_reader_cannot_run_a_model_through_oneshot(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": []},
            headers=_as("reader-token"),
        )
        assert resp.status_code == 403

    async def test_cache_is_admin_only(self, client: AsyncClient) -> None:
        assert (await client.post("/v1/cache/clear", headers=_as("alice-token"))).status_code == 403
        assert (await client.post("/v1/cache/clear", headers=_as("admin-token"))).status_code == 200

    async def test_unknown_credential_is_rejected_before_the_scope_check(
        self, client: AsyncClient
    ) -> None:
        assert (await client.get("/v1/dialects")).status_code == 401
        assert (await client.get("/v1/dialects", headers=_as("nobody"))).status_code == 403


class TestSessionOwnership:
    async def test_other_user_gets_404_on_every_session_route(self, client: AsyncClient) -> None:
        sid, mid = await _session_with_model(client, "alice-token")
        bob = _as("bob-token")
        for resp in (
            await client.get(f"/v1/sessions/{sid}", headers=bob),
            await client.get(f"/v1/sessions/{sid}/models", headers=bob),
            await client.get(f"/v1/sessions/{sid}/models/{mid}/dimensions", headers=bob),
            await client.post(
                f"/v1/sessions/{sid}/query/sql",
                json={
                    "model_id": mid,
                    "query": {"select": {"dimensions": ["Customer Country"]}},
                },
                headers=bob,
            ),
            await client.delete(f"/v1/sessions/{sid}", headers=bob),
        ):
            assert resp.status_code == 404, resp.text
        # Alice still has her session, untouched by Bob's DELETE.
        assert (
            await client.get(f"/v1/sessions/{sid}", headers=_as("alice-token"))
        ).status_code == 200

    async def test_list_shows_own_sessions_and_admin_sees_all(self, client: AsyncClient) -> None:
        alice_sid, _ = await _session_with_model(client, "alice-token")
        bob_sid, _ = await _session_with_model(client, "bob-token")

        async def listed(token: str) -> set[str]:
            resp = await client.get("/v1/sessions", headers=_as(token))
            assert resp.status_code == 200
            return {s["session_id"] for s in resp.json()["sessions"]}

        assert await listed("alice-token") == {alice_sid}
        assert await listed("bob-token") == {bob_sid}
        assert await listed("admin-token") == {alice_sid, bob_sid}

    async def test_shortcuts_resolve_to_the_callers_own_model(self, client: AsyncClient) -> None:
        await _session_with_model(client, "alice-token")
        assert (await client.get("/v1/dimensions", headers=_as("alice-token"))).status_code == 200
        # Bob sees no model at all, rather than Alice's.
        assert (await client.get("/v1/dimensions", headers=_as("bob-token"))).status_code == 404

    async def test_oneshot_cannot_reach_another_users_session(self, client: AsyncClient) -> None:
        sid, mid = await _session_with_model(client, "alice-token")
        resp = await client.post(
            "/v1/oneshot/batch",
            json={
                "session_id": sid,
                "model_id": mid,
                "queries": [
                    {"id": "q1", "query": {"select": {"dimensions": ["Customer Country"]}}}
                ],
            },
            headers=_as("bob-token"),
        )
        assert resp.status_code == 404, resp.text

    async def test_caller_is_unbound_after_the_request(self, client: AsyncClient) -> None:
        await client.post("/v1/sessions", headers=_as("alice-token"))
        assert current_principal() is None
