"""A BI token with a warehouse sign-in, on a data source that logs in as the user (T5).

The whole chain over REST, with a stand-in for the identity provider (code,
refresh and revocation endpoints) and for Databricks (its token exchange and
driver): the page's consent, the popup coming back, the token's creation, a
query with the BI token as its owner, a rotated refresh token, a sign-in the
identity provider ends, and revocation. See
design/PLAN_bi_tokens_user_sources.md.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import (
    _consent_scopes,
    _report_bi_token_sign_in,
    _token_store_keys,
    create_app,
    oidc_config,
)
from orionbelt.api.deps import (
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.auth import AuthConfigError, reset_auth
from orionbelt.auth import consent as consent_module
from orionbelt.cache.file import FileCache
from orionbelt.service import user_login
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.integration.test_api_user_login import MODEL_YAML, QUERY, USER_LOGIN_ENV
from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

pytest.importorskip("ob_flight")

SIGN_IN_AT = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_AT = f"{ISSUER}/protocol/openid-connect/token"
REVOKE_AT = f"{ISSUER}/protocol/openid-connect/revoke"
DATABRICKS_AT = "https://dbc-1234.cloud.databricks.com/oidc/v1/token"


class Services:
    """The identity provider and Databricks, as OBSL's HTTP calls and driver see them."""

    def __init__(self, issuer: FakeIssuer) -> None:
        self.issuer = issuer
        self.codes: list[str] = []
        self.refreshes: list[str] = []  # the refresh tokens OBSL redeemed
        self.revoked: list[tuple[str, Any]] = []
        self.exchanged: list[str] = []  # the subject tokens Databricks got
        self.connections: list[str] = []
        self.refresh_error: tuple[int, str] | None = None
        self.issued = 0

    def post(self, url: str, data: dict[str, str], timeout: float, auth: Any = None) -> Any:
        if url == REVOKE_AT:
            self.revoked.append((data["token"], auth))
            return self._answer(200, {})
        if url == DATABRICKS_AT:
            self.exchanged.append(data["subject_token"])
            return self._answer(200, {"access_token": f"dbx-{len(self.exchanged)}"})
        assert url == TOKEN_AT, url
        assert auth == ("obsl", "obsl-secret")
        if data["grant_type"] == "authorization_code":
            self.codes.append(data["code"])
            return self._tokens()
        assert data["grant_type"] == "refresh_token"
        assert data["scope"] == "openid obsl:write offline_access"
        self.refreshes.append(data["refresh_token"])
        if self.refresh_error is not None:
            status, error = self.refresh_error
            return self._answer(status, {"error": error, "error_description": "says the IdP"})
        return self._tokens()

    def _tokens(self) -> Any:
        self.issued += 1
        return self._answer(
            200,
            {
                "access_token": self.issuer.token("alice", scope="obsl:write"),
                "refresh_token": f"rt-{self.issued}",
                "expires_in": 300,
            },
        )

    @staticmethod
    def _answer(status: int, body: dict[str, Any]) -> Any:
        return SimpleNamespace(status_code=status, json=lambda: body)

    def connect(self, **kwargs: Any) -> Any:
        self.connections.append(kwargs["access_token"])

        class Cursor:
            description = [
                ("Customer Country", "string", None, None, None, None, None),
                ("Total Revenue", "double", None, None, None, None, None),
            ]

            def execute(self, sql: str, *args: Any) -> None:
                self.rows = [("US", 100.0), ("UK", 50.0)]

            def fetchall(self) -> list[tuple[Any, ...]]:
                return self.rows

            def close(self) -> None:
                pass

        return SimpleNamespace(cursor=Cursor, close=lambda: None)


Serve = Callable[..., AbstractAsyncContextManager[tuple[AsyncClient, Services]]]


@pytest.fixture
def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Serve:
    from ob_flight import db_router

    for name, value in USER_LOGIN_ENV.items():
        monkeypatch.setenv(name, value)

    @asynccontextmanager
    async def run(
        *, with_key: bool = True, with_page: bool = True
    ) -> AsyncIterator[tuple[AsyncClient, Services]]:
        issuer = FakeIssuer()
        services = Services(issuer)
        monkeypatch.setattr(user_login.httpx, "post", services.post)  # every module's httpx
        monkeypatch.setitem(
            sys.modules, "fake_ob_databricks", SimpleNamespace(connect=services.connect)
        )
        monkeypatch.setitem(db_router.VENDOR_MAP, "databricks", "fake_ob_databricks")
        user_login.reset_tokens()
        consent_module.consents.clear()
        jwks = tmp_path / "jwks.json"
        jwks.write_text(json.dumps(issuer.jwks()))
        settings = Settings(
            session_ttl_seconds=3600,
            session_cleanup_interval=9999,
            auth_mode="oidc",
            oidc_issuer=ISSUER,
            oidc_audience=AUDIENCE,
            oidc_jwks_file=str(jwks),
            oidc_authorization_endpoint=SIGN_IN_AT,
            oidc_token_endpoint=TOKEN_AT,
            oidc_revocation_endpoint=REVOKE_AT,
            oidc_client_id="obsl",
            oidc_client_secret="obsl-secret",
            oidc_tokens_client_id="obsl-tokens" if with_page else "",
            oidc_tokens_scopes="openid obsl:write",
            token_store_path=str(tmp_path / "bi-tokens.sqlite"),
            token_store_key=(
                base64.b64encode(secrets.token_bytes(32)).decode() if with_key else ""
            ),
        )
        cache = FileCache(
            cache_dir=str(tmp_path / "cache"),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=86400,
            sweep_interval_seconds=3600,
        )
        app = create_app(settings=settings)
        init_session_manager(
            SessionManager(ttl_seconds=3600, cleanup_interval=9999),
            auth_mode="oidc",
            oidc=oidc_config(settings),
            query_execute_enabled=True,
            db_vendor="databricks",
            cache=cache,
            cache_config=CacheRuntimeConfig(
                backend="file",
                min_ttl_seconds=1,
                max_ttl_seconds=86400,
                unknown_policy="default_ttl",
                unknown_default_ttl_seconds=3600,
            ),
        )
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c, services
        finally:
            await cache.shutdown()
            reset_session_manager()
            reset_auth()
            user_login.reset_tokens()
            consent_module.consents.clear()
            db_router.close_all_pools()

    return run


def _alice(services: Services) -> dict[str, str]:
    return {"Authorization": f"Bearer {services.issuer.token('alice', scope='obsl:write')}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _outcome(page: str) -> dict[str, str]:
    found = re.search(r'<script id="outcome" type="application/json">(.*?)</script>', page, re.S)
    assert found is not None
    return dict(json.loads(found.group(1)))


async def _signed_in_consent(client: AsyncClient, services: Services) -> str:
    """What the page and its popup do: start, sign in at the IdP, come back. The id."""
    alice = _alice(services)
    started = await client.post("/v1/auth/tokens/consents", headers=alice)
    assert started.status_code == 201, started.text
    query = {k: v[0] for k, v in parse_qs(urlparse(started.json()["authorize_url"]).query).items()}
    assert query["redirect_uri"] == "http://test/tokens/consent"
    assert query["client_id"] == "obsl"
    back = await client.get("/tokens/consent", params={"code": "code-1", "state": query["state"]})
    assert back.status_code == 200
    assert back.headers["cache-control"] == "no-store"
    consent = started.json()["consent"]
    assert _outcome(back.text) == {"consent": consent}
    status = await client.get(f"/v1/auth/tokens/consents/{consent}", headers=alice)
    assert status.json() == {"consent": consent, "status": "finished", "message": ""}
    return str(consent)


async def _bi_token(client: AsyncClient, services: Services, *, sign_in: bool = True) -> Any:
    body: dict[str, Any] = {"label": "Tableau", "scopes": ["obsl:write"]}
    if sign_in:
        body["consent"] = await _signed_in_consent(client, services)
    created = await client.post("/v1/auth/tokens", json=body, headers=_alice(services))
    assert created.status_code == 201, created.text
    assert created.json()["warehouse_sign_in"] is sign_in
    return created.json()


async def _execute(
    client: AsyncClient, headers: dict[str, str], query: dict[str, Any] = QUERY
) -> Any:
    sid = (await client.post("/v1/sessions", headers=headers)).json()["session_id"]
    loaded = await client.post(
        f"/v1/sessions/{sid}/models", json={"model_yaml": MODEL_YAML}, headers=headers
    )
    assert loaded.status_code == 201, loaded.text
    return await client.post(
        f"/v1/sessions/{sid}/query/execute",
        json={"model_id": loaded.json()["model_id"], "query": query},
        headers=headers,
    )


async def test_a_bi_token_queries_as_its_owner_with_its_warehouse_sign_in(serve: Serve) -> None:
    async with serve() as (client, services):
        created = await _bi_token(client, services)
        bi = _bearer(created["token"])

        first = await _execute(client, bi)
        assert first.status_code == 200, first.text
        assert first.json()["cached"] is False
        # Databricks got Alice's identity provider token, minted from the sign-in,
        # never the BI token.
        (subject,) = services.exchanged
        assert jwt.decode(subject, options={"verify_signature": False})["sub"] == "alice"
        assert services.refreshes == ["rt-1"]
        assert services.connections == ["dbx-1"]

        # The same person, signed in at the IdP: their own cached rows.
        again = await _execute(client, _alice(services))
        assert again.json()["cached"] is True


async def test_a_rotated_refresh_token_is_kept_for_the_next_refresh(serve: Serve) -> None:
    from ob_flight import db_router

    async with serve() as (client, services):
        bi = _bearer((await _bi_token(client, services))["token"])
        assert (await _execute(client, bi)).status_code == 200
        # As if Alice's tokens had expired: no token kept, no connection kept open.
        user_login.reset_tokens()
        db_router.close_all_pools()
        another = {**QUERY, "limit": 1}  # not answered from the cache
        assert (await _execute(client, bi, another)).status_code == 200
        # The second refresh used the token the first one rotated in.
        assert services.refreshes == ["rt-1", "rt-2"]


async def test_a_sign_in_the_idp_ends_is_dropped(serve: Serve) -> None:
    async with serve() as (client, services):
        created = await _bi_token(client, services)
        bi = _bearer(created["token"])
        services.refresh_error = (400, "invalid_grant")

        ended = await _execute(client, bi)
        assert ended.status_code == 401, ended.text
        assert ended.json()["detail"]["code"] == "BI_TOKEN_SIGN_IN_ENDED"
        assert "/tokens" in ended.json()["detail"]["message"]
        listed = (await client.get("/v1/auth/tokens", headers=_alice(services))).json()
        assert [t["warehouse_sign_in"] for t in listed["tokens"]] == [False]

        services.refresh_error = None
        again = await _execute(client, bi)
        assert again.status_code == 401
        assert again.json()["detail"]["code"] == "SIGN_IN_REQUIRED"
        assert services.refreshes == ["rt-1"]  # not asked again
        assert services.exchanged == []


async def test_obsl_s_own_client_refused_keeps_the_sign_in(serve: Serve) -> None:
    async with serve() as (client, services):
        bi = _bearer((await _bi_token(client, services))["token"])
        services.refresh_error = (401, "invalid_client")
        refused = await _execute(client, bi)
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "DATA_SOURCE_SIGN_IN_REFUSED"
        services.refresh_error = None
        assert (await _execute(client, bi)).status_code == 200


async def test_revoking_the_token_revokes_its_sign_in_at_the_idp(serve: Serve) -> None:
    async with serve() as (client, services):
        created = await _bi_token(client, services)
        revoked = await client.delete(f"/v1/auth/tokens/{created['id']}", headers=_alice(services))
        assert revoked.status_code == 204
        assert services.revoked == [("rt-1", ("obsl", "obsl-secret"))]
        refused = await client.post("/v1/sessions", headers=_bearer(created["token"]))
        assert refused.status_code == 401  # the BI token itself is revoked


async def test_a_bi_token_created_without_a_sign_in_is_told_to_make_a_new_one(
    serve: Serve,
) -> None:
    async with serve() as (client, services):
        created = await _bi_token(client, services, sign_in=False)
        refused = await _execute(client, _bearer(created["token"]))
        assert refused.status_code == 401
        assert refused.json()["detail"]["code"] == "SIGN_IN_REQUIRED"
        assert "create a new BI token" in refused.json()["detail"]["message"]
        assert services.exchanged == []


async def test_only_the_signed_in_user_starts_and_uses_a_sign_in(serve: Serve) -> None:
    async with serve() as (client, services):
        created = await _bi_token(client, services, sign_in=False)
        refused = await client.post("/v1/auth/tokens/consents", headers=_bearer(created["token"]))
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "BI_TOKEN_NEEDS_SIGN_IN"

        consent = await _signed_in_consent(client, services)
        bob = _bearer(services.issuer.token("bob", scope="obsl:write"))
        assert (
            await client.get(f"/v1/auth/tokens/consents/{consent}", headers=bob)
        ).status_code == 404
        taken = await client.post(
            "/v1/auth/tokens", json={"label": "x", "consent": consent}, headers=bob
        )
        assert taken.status_code == 422
        assert taken.json()["detail"]["code"] == "CONSENT_NOT_FOUND"
        # Still Alice's to use.
        mine = await client.post(
            "/v1/auth/tokens",
            json={"label": "x", "consent": consent},
            headers=_alice(services),
        )
        assert mine.status_code == 201
        assert mine.json()["warehouse_sign_in"] is True


async def test_the_api_flow_needs_no_token_page(serve: Serve) -> None:
    """A client of its own runs the steps the page would: the callback is there for it."""
    async with serve(with_page=False) as (client, services):
        assert (await client.get("/tokens")).status_code == 404
        created = await _bi_token(client, services)  # start, callback, status, create
        assert (await _execute(client, _bearer(created["token"]))).status_code == 200
        assert (await client.get("/tokens/consent.js")).status_code == 200
        assert (await client.get("/tokens/app.css")).status_code == 200
        assert (await client.get("/tokens/app.js")).status_code == 404  # the page's own


async def test_a_sign_in_cancelled_at_the_idp_says_so(serve: Serve) -> None:
    async with serve() as (client, services):
        started = await client.post("/v1/auth/tokens/consents", headers=_alice(services))
        state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]
        back = await client.get(
            "/tokens/consent",
            params={"error": "access_denied", "error_description": "User denied", "state": state},
        )
        assert _outcome(back.text) == {"error": "Sign-in failed: User denied"}
        status = await client.get(
            f"/v1/auth/tokens/consents/{started.json()['consent']}", headers=_alice(services)
        )
        assert status.json()["status"] == "failed"
        assert services.codes == []


async def test_without_a_key_bi_tokens_hold_no_sign_in(serve: Serve) -> None:
    async with serve(with_key=False) as (client, services):
        refused = await client.post("/v1/auth/tokens/consents", headers=_alice(services))
        assert refused.status_code == 404
        assert refused.json()["detail"]["code"] == "WAREHOUSE_SIGN_IN_OFF"
        callback = await client.get("/tokens/consent", params={"state": "s", "code": "c"})
        assert callback.status_code == 404
        page = await client.get("/tokens")
        assert '"warehouse_sign_in": false' in page.text

        created = await _bi_token(client, services, sign_in=False)
        answer = await _execute(client, _bearer(created["token"]))
        assert answer.status_code == 401
        assert "TOKEN_STORE_KEY" in answer.json()["detail"]["message"]


class TestSettings:
    def test_the_key_comes_from_the_setting_or_its_file(self, tmp_path: Path) -> None:
        key = secrets.token_bytes(32)
        encoded = base64.b64encode(key).decode()
        assert _token_store_keys(Settings(token_store_key=encoded)) == (key,)
        key_file = tmp_path / "token-store-key"
        key_file.write_text(f"{encoded}\n")
        assert _token_store_keys(Settings(token_store_key_file=str(key_file))) == (key,)
        assert _token_store_keys(Settings()) == ()

    def test_a_wrong_key_setting_refuses_to_start(self, tmp_path: Path) -> None:
        encoded = base64.b64encode(secrets.token_bytes(32)).decode()
        key_file = tmp_path / "token-store-key"
        key_file.write_text(encoded)
        for settings, match in (
            (Settings(token_store_key=encoded, token_store_key_file=str(key_file)), "not both"),
            (Settings(token_store_key_file=str(tmp_path / "absent")), "cannot be read"),
            (Settings(token_store_key="c2hvcnQ="), "entry 1 is not"),
        ):
            with pytest.raises(AuthConfigError, match=match):
                _token_store_keys(settings)

    def test_the_sign_in_asks_for_the_page_s_scopes_and_offline_access(self) -> None:
        assert _consent_scopes(Settings(oidc_tokens_scopes="openid obsl:read")) == (
            "openid obsl:read offline_access"
        )
        assert _consent_scopes(Settings(oidc_tokens_scopes="openid offline_access")) == (
            "openid offline_access"
        )
        explicit = "openid api://obsl/obsl.read offline_access"
        assert _consent_scopes(Settings(oidc_consent_scopes=explicit)) == explicit

    async def test_startup_says_what_a_sign_in_still_needs(
        self, serve: Serve, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with serve(with_key=False):
            with caplog.at_level("WARNING", logger="orionbelt.api"):
                _report_bi_token_sign_in()
        assert "refuse BI tokens" in caplog.text
        assert "TOKEN_STORE_KEY" in caplog.text
