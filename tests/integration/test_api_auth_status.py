"""How authentication is set up: ``GET /v1/auth/status``, the startup lines, key ids.

See design/PLAN_auth_phase6.md (A1-A4).
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import (
    OidcClient,
    OidcConfig,
    get_key_last_used,
    get_token_store,
    init_auth,
    note_key_used,
    reset_auth,
)
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.auth.keys import key_id
from orionbelt.auth.pgwire_login import login_summary
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

KEY = "obsl_pat_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
OTHER_KEY = "obsl_pat_" + "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
CLIENT_SECRET = "obsl-client-secret-value"
STORE_KEY = base64.b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_auth()


@asynccontextmanager
async def _client(
    settings: Settings, flight_info: dict[str, object] | None = None
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode=settings.auth_mode,
        api_keys=settings.api_keys,
        oidc=oidc_config(settings),
        flight_info=flight_info,
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        reset_session_manager()


def _no_secret_in(text: str) -> None:
    for secret in (KEY, OTHER_KEY, CLIENT_SECRET, STORE_KEY):
        assert secret not in text


class TestApiKeyMode:
    async def test_keys_appear_by_id_with_their_last_use(self) -> None:
        settings = Settings(auth_mode="api_key", api_keys=f"{KEY},{OTHER_KEY}")
        async with _client(settings) as client:
            answer = await client.get("/v1/auth/status", headers={"X-API-Key": KEY})
        assert answer.status_code == 200, answer.text
        status = answer.json()
        assert status["mode"] == "api_key"
        assert status["api_key_header"] == "X-API-Key"
        used = {k["id"]: k["last_used_since_start"] for k in status["service_keys"]}
        assert set(used) == {key_id(KEY), key_id(OTHER_KEY)}
        assert used[key_id(KEY)] is not None  # this request
        assert used[key_id(OTHER_KEY)] is None
        assert status["oidc"] is None
        assert status["bi_tokens"] == {
            "enabled": False,
            "store": None,
            "active": 0,
            "with_warehouse_sign_in": 0,
            "default_days": None,
            "max_days": None,
        }
        assert status["warehouse_sign_in"]["ready"] is False
        assert "AUTH_MODE=oidc" in status["warehouse_sign_in"]["missing"]
        _no_secret_in(answer.text)

    async def test_flight_is_on_only_once_it_started(self) -> None:
        """FLIGHT_ENABLED stays true when the start was skipped (no ob-flight-extension)."""
        settings = Settings(auth_mode="api_key", api_keys=KEY, flight_enabled=True)
        async with _client(settings) as client:
            skipped = (await client.get("/v1/auth/status", headers={"X-API-Key": KEY})).json()
        assert skipped["surfaces"]["flight"] == {"enabled": False, "tls": False, "login": None}
        started = {"enabled": True, "port": 8815, "tls": True}
        async with _client(settings, flight_info=started) as client:
            running = (await client.get("/v1/auth/status", headers={"X-API-Key": KEY})).json()
        assert running["surfaces"]["flight"] == {
            "enabled": True,
            "tls": True,
            "login": "AUTH_MODE=api_key",
        }

    async def test_it_is_in_the_api_document(self) -> None:
        async with _client(Settings(auth_mode="api_key", api_keys=KEY)) as client:
            document = await client.get("/openapi.json", headers={"X-API-Key": KEY})
        assert "/v1/auth/status" in document.json()["paths"]


USER_LOGIN_ENV = {
    "OBSL_DATABASES": "crm",
    "DB_CRM_TYPE": "databricks",
    "DB_CRM_AUTH": "user",
    "DB_CRM_DATABRICKS_SERVER_HOSTNAME": "dbc-1234.cloud.databricks.com",
}


class TestOidcMode:
    @pytest.fixture
    def settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
        pytest.importorskip("ob_flight")
        for name, value in USER_LOGIN_ENV.items():
            monkeypatch.setenv(name, value)
        self.issuer = FakeIssuer()
        jwks = tmp_path / "jwks.json"
        jwks.write_text(json.dumps(self.issuer.jwks()))
        return Settings(
            auth_mode="oidc",
            api_keys=KEY,
            oidc_issuer=ISSUER,
            oidc_audience=AUDIENCE,
            oidc_jwks_file=str(jwks),
            oidc_authorization_endpoint=f"{ISSUER}/auth",
            oidc_token_endpoint=f"{ISSUER}/token",
            oidc_scope_map="analysts=obsl:read",
            oidc_client_id="obsl",
            oidc_client_secret=CLIENT_SECRET,
            oidc_tokens_client_id="obsl-tokens",
            oidc_swagger_client_id="obsl-docs",
            token_store_path=str(tmp_path / "bi.sqlite"),
            token_store_key=STORE_KEY,
            pgwire_enabled=True,
        )

    async def test_an_admin_sees_the_whole_setup(self, settings: Settings) -> None:
        admin = {"Authorization": f"Bearer {self.issuer.token('root', scope='obsl:admin')}"}
        async with _client(settings) as client:
            store = get_token_store()
            assert store is not None
            store.create(
                owner_sub="alice",
                owner_email=None,
                label="Tableau",
                scopes=frozenset({"obsl:read"}),
                days=90,
                refresh_token="rt-alice",
            )
            store.create(
                owner_sub="alice", owner_email=None, label="DBeaver", scopes=frozenset(), days=90
            )
            answer = await client.get("/v1/auth/status", headers=admin)
        assert answer.status_code == 200, answer.text
        status = answer.json()
        assert status["mode"] == "oidc"
        oidc = status["oidc"]
        assert (oidc["issuer"], oidc["audience"], oidc["client_id"]) == (ISSUER, AUDIENCE, "obsl")
        assert oidc["authorization_endpoint"] == f"{ISSUER}/auth"
        assert oidc["token_endpoint"] == f"{ISSUER}/token"
        assert oidc["signing_keys"]["source"] == "OIDC_JWKS_FILE"
        assert oidc["signing_keys"]["count"] == 1
        assert oidc["signing_keys"]["loaded_at"] is not None
        assert oidc["scope_map"] == {"analysts": ["obsl:read"]}
        assert [k["id"] for k in status["service_keys"]] == [key_id(KEY)]
        bi = status["bi_tokens"]
        assert (bi["enabled"], bi["active"], bi["with_warehouse_sign_in"]) == (True, 2, 1)
        assert status["warehouse_sign_in"] == {"ready": True, "missing": []}
        assert status["user_login_sources"] == [
            {"name": "crm", "login": "databricks, token federation"}
        ]
        surfaces = status["surfaces"]
        assert surfaces["tokens_page"] is True and surfaces["swagger_sign_in"] is True
        assert surfaces["pgwire"] == {
            "enabled": True,
            "tls": False,
            "login": "AUTH_MODE=oidc, SCRAM-SHA-256",
        }
        assert surfaces["flight"]["enabled"] is False
        _no_secret_in(answer.text)
        assert "rt-alice" not in answer.text

    @pytest.mark.parametrize(
        "change",
        [
            {"expose_api_docs": False},
            {"expose_openapi_schema": False},
            {"oidc_authorization_endpoint": "", "oidc_token_endpoint": ""},
        ],
    )
    async def test_docs_sign_in_only_when_docs_offer_it(
        self, settings: Settings, change: dict[str, object]
    ) -> None:
        admin = {"Authorization": f"Bearer {self.issuer.token('root', scope='obsl:admin')}"}
        async with _client(settings.model_copy(update=change)) as client:
            status = (await client.get("/v1/auth/status", headers=admin)).json()
        assert status["surfaces"]["swagger_sign_in"] is False

    async def test_only_an_admin(self, settings: Settings) -> None:
        reader = {"Authorization": f"Bearer {self.issuer.token('alice', scope='obsl:read')}"}
        async with _client(settings) as client:
            refused = await client.get("/v1/auth/status", headers=reader)
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "AUTH_INSUFFICIENT_SCOPE"


class TestStartupLines:
    def test_api_keys_are_named_by_id(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="orionbelt.auth"):
            init_auth(auth_mode="api_key", api_keys=f"{KEY},{OTHER_KEY}")
        assert f"2 key(s) ({', '.join(sorted([key_id(KEY), key_id(OTHER_KEY)]))})" in caplog.text
        assert KEY not in caplog.text and KEY[:12] not in caplog.text

    def test_oidc_says_what_it_verifies_and_the_bi_tokens(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = OidcConfig(
            issuer=ISSUER, audience=AUDIENCE, token_store_path=str(tmp_path / "bi.sqlite")
        )
        with caplog.at_level(logging.INFO, logger="orionbelt.auth"):
            init_auth(
                auth_mode="oidc",
                oidc=config,
                verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
            )
        assert f"Auth: oidc; issuer {ISSUER}; audience {AUDIENCE}; 0 service key(s)" in caplog.text
        assert "BI tokens on (store" in caplog.text
        assert "0 active, 0 with a warehouse sign-in" in caplog.text
        assert "warehouse sign-ins need TOKEN_STORE_KEY" in caplog.text
        assert not (tmp_path / "bi.sqlite").exists()  # counting creates no store

    @pytest.mark.parametrize("with_client", [False, True])
    def test_sign_ins_are_ready_only_with_every_piece(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, with_client: bool
    ) -> None:
        config = OidcConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            token_store_path=str(tmp_path / "bi.sqlite"),
            token_store_keys=(secrets.token_bytes(32),),
            authorization_endpoint=f"{ISSUER}/auth",
            token_endpoint=f"{ISSUER}/token",
            client=OidcClient(client_id="obsl", client_secret=CLIENT_SECRET)
            if with_client
            else None,
        )
        with caplog.at_level(logging.INFO, logger="orionbelt.auth"):
            init_auth(
                auth_mode="oidc",
                oidc=config,
                verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
            )
        if with_client:
            assert "warehouse sign-ins can be made" in caplog.text
        else:
            assert "warehouse sign-ins need OIDC_CLIENT_ID and OIDC_CLIENT_SECRET" in caplog.text
            assert "can be made" not in caplog.text


class TestKeyUse:
    def test_a_scram_login_s_key_is_recorded(self) -> None:
        init_auth(auth_mode="api_key", api_keys=KEY)
        assert get_key_last_used() == {key_id(KEY): None}
        note_key_used(KEY)
        note_key_used("not a configured key")
        assert get_key_last_used()[key_id(KEY)] is not None


@pytest.mark.parametrize(
    ("auth_mode", "pgwire_auth_mode", "tls", "expected"),
    [
        ("none", "trust", False, "AUTH_MODE=none, no password"),
        ("api_key", "trust", False, "AUTH_MODE=api_key, SCRAM-SHA-256"),
        ("api_key", "password", False, "AUTH_MODE=api_key, cleartext password"),
    ],
)
def test_pgwire_says_how_clients_log_in(
    auth_mode: str, pgwire_auth_mode: str, tls: bool, expected: str
) -> None:
    init_auth(auth_mode=auth_mode, api_keys=KEY if auth_mode != "none" else "")
    assert login_summary(pgwire_auth_mode, tls=tls) == expected


def test_pgwire_over_tls_under_oidc_takes_cleartext(tmp_path: Path) -> None:
    config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)
    init_auth(
        auth_mode="oidc",
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
    )
    assert login_summary("trust", tls=True) == "AUTH_MODE=oidc, cleartext password"
