"""OBSL's token exchange against a real identity provider: Keycloak, in a container.

A Snowflake source with ``DB_<NAME>_OAUTH_EXCHANGE=token_exchange`` asks the
identity provider for the user's Snowflake token with the standard token
exchange (RFC 8693). Here Keycloak answers it: the user's token, issued for
OBSL's client, comes back issued for Snowflake, for the same user, carrying
the role scope Snowflake reads, and signed so Snowflake can check it.
``pytest -m docker``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest

pytest.importorskip("ob_flight")
pytest.importorskip("testcontainers")

from ob_flight.datasources import DataSource, configured_datasources  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402

from orionbelt.auth import (  # noqa: E402
    OidcClient,
    OidcConfig,
    authenticate,
    bind_principal,
    init_auth,
    reset_auth,
    reset_principal,
)
from orionbelt.service import user_login  # noqa: E402
from orionbelt.service.user_login import SignInRefusedError  # noqa: E402

pytestmark = pytest.mark.docker

IMAGE = "quay.io/keycloak/keycloak:26.8.2"
ADMIN_PASSWORD = "integration-admin"
# Characters OBSL must form-encode for HTTP Basic (RFC 6749 2.3.1): Keycloak
# refuses the secret sent raw, so a pass here proves the encoding.
CLIENT_SECRET = "obsl+secret/with space%"
ROLE_SCOPE = "session:role:ANALYST"
ALICE = ("alice", "alice-integration-password")


@pytest.fixture(scope="module")
def keycloak() -> Iterator[str]:
    """Keycloak with realm ``obsl``; its base URL."""
    container = (
        DockerContainer(IMAGE)
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", ADMIN_PASSWORD)
        .with_command("start-dev")
        .with_exposed_ports(8080)
    )
    container.start()
    try:
        base = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        deadline = time.monotonic() + 180
        while not _serving(f"{base}/realms/master"):
            if time.monotonic() > deadline:
                pytest.fail(f"Keycloak not serving: {container.get_logs()}")
            time.sleep(1)
        _configure(base)
        yield base
    finally:
        container.stop()


def _serving(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _configure(base: str) -> None:
    """Realm ``obsl``: OBSL's client, a login client for the test, Snowflake, Alice.

    ``obsl`` is OBSL's confidential client with standard token exchange on.
    The user's token is issued for it (``obsl-login`` adds it to ``aud``),
    which Keycloak requires of the subject token. Its optional client scope
    ``session:role:ANALYST`` adds Snowflake as an audience and the ``scp``
    claim Snowflake reads the role from.
    """
    admin = httpx.post(
        f"{base}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": ADMIN_PASSWORD,
        },
    ).json()["access_token"]
    api = httpx.Client(
        base_url=f"{base}/admin/realms", headers={"Authorization": f"Bearer {admin}"}
    )

    def call(method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = api.request(method, path, **kwargs)
        assert response.status_code < 400, f"{method} {path}: {response.text}"
        return response

    def audience(name: str, client_id: str) -> dict[str, Any]:
        return {
            "name": name,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": client_id, "access.token.claim": "true"},
        }

    call("POST", "", json={"realm": "obsl", "enabled": True})
    call("POST", "/obsl/clients", json={"clientId": "snowflake", "publicClient": False})
    call(
        "POST",
        "/obsl/clients",
        json={
            "clientId": "obsl",
            "publicClient": False,
            "secret": CLIENT_SECRET,
            "standardFlowEnabled": False,
            "attributes": {"standard.token.exchange.enabled": "true"},
        },
    )
    call(
        "POST",
        "/obsl/clients",
        json={
            "clientId": "obsl-login",
            "publicClient": True,
            "directAccessGrantsEnabled": True,
            "protocolMappers": [audience("aud-obsl", "obsl")],
        },
    )
    call(
        "POST",
        "/obsl/client-scopes",
        json={
            "name": ROLE_SCOPE,
            "protocol": "openid-connect",
            "protocolMappers": [
                audience("aud-snowflake", "snowflake"),
                {
                    "name": "scp",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-hardcoded-claim-mapper",
                    "config": {
                        "claim.name": "scp",
                        "claim.value": f'["{ROLE_SCOPE}"]',
                        "jsonType.label": "JSON",
                        "access.token.claim": "true",
                    },
                },
            ],
        },
    )
    scope_id = next(
        s["id"] for s in call("GET", "/obsl/client-scopes").json() if s["name"] == ROLE_SCOPE
    )
    obsl_id = call("GET", "/obsl/clients", params={"clientId": "obsl"}).json()[0]["id"]
    call("PUT", f"/obsl/clients/{obsl_id}/optional-client-scopes/{scope_id}")
    call(
        "POST",
        "/obsl/users",
        json={
            "username": ALICE[0],
            "email": "alice@obsl.test",
            "emailVerified": True,
            "firstName": "Alice",
            "lastName": "Test",
            "enabled": True,
            "credentials": [{"type": "password", "value": ALICE[1], "temporary": False}],
        },
    )


@pytest.fixture
def issuer(keycloak: str) -> Iterator[str]:
    """``AUTH_MODE=oidc`` against the realm, with OBSL's client there."""
    issuer = f"{keycloak}/realms/obsl"
    init_auth(
        auth_mode="oidc",
        oidc=OidcConfig(
            issuer=issuer,
            audience="obsl",
            client=OidcClient(client_id="obsl", client_secret=CLIENT_SECRET),
        ),
    )
    user_login.reset_tokens()
    yield issuer
    reset_auth()
    user_login.reset_tokens()


def _snowflake_source(**overrides: str) -> DataSource:
    env = {
        "OBSL_DATABASES": "lake",
        "DB_LAKE_TYPE": "snowflake",
        "DB_LAKE_AUTH": "user",
        "DB_LAKE_OAUTH_EXCHANGE": "token_exchange",
        "DB_LAKE_OAUTH_SCOPE": ROLE_SCOPE,
        "DB_LAKE_OAUTH_AUDIENCE": "snowflake",
        "DB_LAKE_SNOWFLAKE_ACCOUNT": "xy12345",
    }
    (source,) = configured_datasources({**env, **overrides})
    return source


def _as_alice(issuer: str, source: DataSource) -> tuple[str, str]:
    """Sign Alice in, have OBSL accept her token, and exchange it: (her sub, the result)."""
    token = httpx.post(
        f"{issuer}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "obsl-login",
            "username": ALICE[0],
            "password": ALICE[1],
        },
    ).json()["access_token"]
    principal = authenticate(token)
    assert principal.sub is not None
    binding = bind_principal(principal, token)
    try:
        return principal.sub, user_login.database_token(source)
    finally:
        reset_principal(binding)


def test_the_users_token_comes_back_issued_for_snowflake(issuer: str) -> None:
    sub, issued = _as_alice(issuer, _snowflake_source())
    key = jwt.PyJWKClient(f"{issuer}/protocol/openid-connect/certs").get_signing_key_from_jwt(
        issued
    )
    claims = jwt.decode(issued, key.key, algorithms=["RS256"], audience="snowflake", issuer=issuer)
    assert claims["sub"] == sub  # the same user
    assert claims["email"] == "alice@obsl.test"  # what Snowflake maps to its user
    assert claims["scp"] == [ROLE_SCOPE]  # the role Snowflake grants
    assert claims["azp"] == "obsl"  # asked for by OBSL's client


@pytest.mark.parametrize(
    "overrides",
    [
        {"DB_LAKE_OAUTH_SCOPE": "session:role:ACCOUNTADMIN"},  # a scope OBSL is not given
        {"DB_LAKE_OAUTH_AUDIENCE": "someone-else"},  # an audience it may not ask for
    ],
)
def test_what_obsl_may_not_ask_for_is_refused(issuer: str, overrides: dict[str, str]) -> None:
    with pytest.raises(SignInRefusedError):
        _as_alice(issuer, _snowflake_source(**overrides))
