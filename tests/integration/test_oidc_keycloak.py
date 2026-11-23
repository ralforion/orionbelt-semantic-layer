"""AUTH_MODE=oidc against a real Keycloak. Opt-in: set OBSL_TEST_KEYCLOAK_URL.

Start a throwaway Keycloak (admin / admin) and point the test at it:

    docker run --rm -p 18080:8080 \\
      -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \\
      quay.io/keycloak/keycloak:26.0 start-dev
    OBSL_TEST_KEYCLOAK_URL=http://localhost:18080 \\
      uv run pytest tests/integration/test_oidc_keycloak.py

The test creates its own realm with two service-account clients (one granted
the realm role ``obsl-readers``, one ``obsl-admins``), an audience mapper for
``obsl-api``, and deletes the realm afterwards. OBSL discovers the realm's
keys over HTTP and verifies the client-credentials tokens Keycloak issues.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

KEYCLOAK = os.environ.get("OBSL_TEST_KEYCLOAK_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not KEYCLOAK, reason="set OBSL_TEST_KEYCLOAK_URL to a Keycloak (admin/admin) to run"
)
AUDIENCE = "obsl-api"


@dataclass(frozen=True)
class Realm:
    name: str
    issuer: str
    reader_token: str
    admin_token: str


def _client(admin: httpx.Client, realm: str, client_id: str, role: str) -> str:
    """Create a service-account client holding realm role ``role``; return its token."""
    secret = secrets.token_hex(16)
    admin.post(
        f"/admin/realms/{realm}/clients",
        json={
            "clientId": client_id,
            "secret": secret,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "protocolMappers": [
                {
                    "name": "obsl-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {
                        "included.custom.audience": AUDIENCE,
                        "access.token.claim": "true",
                    },
                }
            ],
        },
    ).raise_for_status()
    uuid = admin.get(f"/admin/realms/{realm}/clients", params={"clientId": client_id}).json()[0][
        "id"
    ]
    user = admin.get(f"/admin/realms/{realm}/clients/{uuid}/service-account-user").json()
    role_repr = admin.get(f"/admin/realms/{realm}/roles/{role}").json()
    admin.post(
        f"/admin/realms/{realm}/users/{user['id']}/role-mappings/realm", json=[role_repr]
    ).raise_for_status()
    token = httpx.post(
        f"{KEYCLOAK}/realms/{realm}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
        timeout=10,
    )
    token.raise_for_status()
    return str(token.json()["access_token"])


@pytest.fixture(scope="module")
def realm() -> Iterator[Realm]:
    login = httpx.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": "admin",
        },
        timeout=10,
    )
    login.raise_for_status()
    name = f"obsl-test-{secrets.token_hex(4)}"
    with httpx.Client(
        base_url=KEYCLOAK,
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        timeout=10,
    ) as admin:
        admin.post("/admin/realms", json={"realm": name, "enabled": True}).raise_for_status()
        try:
            for role in ("obsl-readers", "obsl-admins"):
                admin.post(f"/admin/realms/{name}/roles", json={"name": role}).raise_for_status()
            yield Realm(
                name=name,
                issuer=f"{KEYCLOAK}/realms/{name}",
                reader_token=_client(admin, name, "obsl-reader", "obsl-readers"),
                admin_token=_client(admin, name, "obsl-admin", "obsl-admins"),
            )
        finally:
            admin.delete(f"/admin/realms/{name}")


@pytest.fixture
def app(realm: Realm) -> Iterator[FastAPI]:
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        auth_mode="oidc",
        oidc_issuer=realm.issuer,
        oidc_audience=AUDIENCE,
        oidc_scope_claims="scope,realm_access.roles",
        oidc_scope_map="obsl-readers=obsl:read,obsl-admins=obsl:admin",
    )
    application = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode=settings.auth_mode,
        oidc=oidc_config(settings),  # discovery and keys over HTTP, as deployed
    )
    yield application
    reset_session_manager()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_reader_token_reads(client: AsyncClient, realm: Realm) -> None:
    headers = {"Authorization": f"Bearer {realm.reader_token}"}
    assert (await client.get("/v1/dialects", headers=headers)).status_code == 200
    assert (await client.post("/v1/cache/clear", headers=headers)).status_code == 403


async def test_admin_token_reaches_the_cache(client: AsyncClient, realm: Realm) -> None:
    headers = {"Authorization": f"Bearer {realm.admin_token}"}
    assert (await client.post("/v1/cache/clear", headers=headers)).status_code == 200


async def test_sessions_belong_to_the_service_account(client: AsyncClient, realm: Realm) -> None:
    admin = {"Authorization": f"Bearer {realm.admin_token}"}
    reader = {"Authorization": f"Bearer {realm.reader_token}"}
    sid = (await client.post("/v1/sessions", headers=reader)).json()["session_id"]
    assert (await client.get(f"/v1/sessions/{sid}", headers=reader)).status_code == 200
    # The admin client is a different subject, and sees it as admin.
    assert (await client.get(f"/v1/sessions/{sid}", headers=admin)).status_code == 200


async def test_tampered_token_is_refused(client: AsyncClient, realm: Realm) -> None:
    head, payload, signature = realm.reader_token.split(".")
    flipped = signature[:-2] + ("A" if signature[-2] != "A" else "B") + signature[-1]
    resp = await client.get(
        "/v1/dialects", headers={"Authorization": f"Bearer {head}.{payload}.{flipped}"}
    )
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]
