"""How a data source that logs in as the user answers on REST and Flight, and at startup."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import _refuse_unusable_user_login, create_app, oidc_config
from orionbelt.auth import AuthConfigError, OidcClient, OidcConfig, init_auth, reset_auth
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.service import user_login
from orionbelt.settings import Settings
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

pytest.importorskip("ob_flight")

USER_LOGIN_ENV = {
    "OBSL_DATABASES": "crm",
    "DB_CRM_TYPE": "databricks",
    "DB_CRM_AUTH": "user",
    "DB_CRM_DATABRICKS_SERVER_HOSTNAME": "dbc-1234.cloud.databricks.com",
}


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_auth()


class TestRest:
    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            ("SignInRequiredError", 401, "SIGN_IN_REQUIRED"),
            ("SignInRefusedError", 403, "DATA_SOURCE_SIGN_IN_REFUSED"),
            ("SignInUnavailableError", 503, "DATA_SOURCE_SIGN_IN_UNAVAILABLE"),
            ("SignInInteractionRequiredError", 401, "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED"),
        ],
    )
    async def test_a_sign_in_failure_has_its_status_and_code(
        self, error: str, status: int, code: str
    ) -> None:
        app = create_app(settings=Settings())

        @app.get("/probe")
        async def probe() -> None:
            raise getattr(user_login, error)("the reason")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/probe")
        assert resp.status_code == status
        assert resp.json() == {"detail": {"code": code, "message": "the reason"}}
        if status == 401:
            assert resp.headers["www-authenticate"] == 'Bearer realm="obsl"'

    @pytest.mark.parametrize(
        "explicit", ["https://login.example.test/tenant-id/oauth2/v2.0/authorize", ""]
    )
    async def test_the_identity_providers_claims_challenge_reaches_the_client(
        self, explicit: str
    ) -> None:
        """Entra asks the caller to sign in again with these claims (MFA, conditional
        access); its guidance is a 401 whose WWW-Authenticate carries them, and where
        to sign in (``OIDC_AUTHORIZATION_ENDPOINT``, else the discovered one)."""
        claims = '{"access_token":{"polids":{"essential":true,"values":["p1"]}}}'
        app = create_app(settings=Settings())
        config = OidcConfig(issuer=ISSUER, audience=AUDIENCE, authorization_endpoint=explicit)
        init_auth(
            auth_mode="oidc",
            oidc=config,
            verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
        )

        @app.get("/probe")
        async def probe() -> None:
            raise user_login.SignInInteractionRequiredError("sign in again", claims=claims)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/probe")
        assert resp.status_code == 401
        sign_in_at = explicit or FakeIssuer().discovery()["authorization_endpoint"]
        encoded = base64.b64encode(claims.encode()).decode()
        assert resp.headers["www-authenticate"] == (
            f'Bearer realm="obsl", authorization_uri="{sign_in_at}", '
            f'error="insufficient_claims", claims="{encoded}"'
        )


class TestStartup:
    def test_a_user_login_source_needs_oidc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in USER_LOGIN_ENV.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(AuthConfigError, match="crm log in as the user"):
            _refuse_unusable_user_login()

    def test_with_oidc_it_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in USER_LOGIN_ENV.items():
            monkeypatch.setenv(name, value)
        config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)
        init_auth(
            auth_mode="oidc",
            oidc=config,
            verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
        )
        _refuse_unusable_user_login()

    def test_a_configuration_error_is_left_to_where_the_source_is_used(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("OBSL_DATABASES", "crm")  # no DB_CRM_TYPE
        _refuse_unusable_user_login()
        assert "has no type" in caplog.text


SNOWFLAKE_ENV = {
    "OBSL_DATABASES": "lake",
    "DB_LAKE_TYPE": "snowflake",
    "DB_LAKE_AUTH": "user",
    "DB_LAKE_OAUTH_EXCHANGE": "entra_obo",
    "DB_LAKE_OAUTH_SCOPE": "api://snowflake-app/session:role-any",
    "DB_LAKE_SNOWFLAKE_ACCOUNT": "xy12345",
}
CLIENT = OidcClient(client_id="obsl-app", client_secret="the-obsl-client-secret")


class TestSnowflakeStartup:
    """A Snowflake user login needs OBSL's own client at the identity provider, and
    the token endpoint it asks there."""

    def _oidc(self, *, client: OidcClient | None, jwks_file: str = "") -> None:
        config = OidcConfig(issuer=ISSUER, audience=AUDIENCE, jwks_file=jwks_file, client=client)
        init_auth(
            auth_mode="oidc",
            oidc=config,
            verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
        )

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in SNOWFLAKE_ENV.items():
            monkeypatch.setenv(name, value)

    def test_without_obsl_s_client_it_refuses(self) -> None:
        self._oidc(client=None)
        with pytest.raises(AuthConfigError, match="lake .*set OIDC_CLIENT_ID"):
            _refuse_unusable_user_login()

    def test_without_a_token_endpoint_it_refuses(self, tmp_path: Path) -> None:
        jwks = tmp_path / "jwks.json"
        jwks.write_text(json.dumps(FakeIssuer().jwks()))  # no discovery document read
        self._oidc(client=CLIENT, jwks_file=str(jwks))
        with pytest.raises(AuthConfigError, match="set OIDC_TOKEN_ENDPOINT"):
            _refuse_unusable_user_login()

    def test_with_both_it_starts(self) -> None:
        self._oidc(client=CLIENT)
        _refuse_unusable_user_login()


class TestObslClient:
    """``OIDC_CLIENT_ID`` with ``OIDC_CLIENT_SECRET`` or ``OIDC_CLIENT_SECRET_FILE``."""

    @staticmethod
    def _client(**settings: Any) -> OidcClient | None:
        config = oidc_config(
            Settings(auth_mode="oidc", oidc_issuer=ISSUER, oidc_audience=AUDIENCE, **settings)
        )
        assert config is not None
        return config.client

    def test_none_by_default(self) -> None:
        assert self._client() is None

    def test_from_the_secret(self) -> None:
        client = self._client(oidc_client_id="obsl-app", oidc_client_secret="s3cret-value")
        assert client == OidcClient(client_id="obsl-app", client_secret="s3cret-value")
        assert "s3cret-value" not in repr(client)

    def test_from_a_file(self, tmp_path: Path) -> None:
        secret = tmp_path / "client-secret"
        secret.write_text("s3cret-value\n")
        client = self._client(oidc_client_id="obsl-app", oidc_client_secret_file=str(secret))
        assert client is not None
        assert client.client_secret == "s3cret-value"

    @pytest.mark.parametrize(
        ("settings", "match"),
        [
            ({"oidc_client_secret": "s3cret-value"}, "without OIDC_CLIENT_ID"),
            ({"oidc_client_id": "obsl-app"}, "without OIDC_CLIENT_SECRET"),
            (
                {
                    "oidc_client_id": "obsl-app",
                    "oidc_client_secret": "s3cret-value",
                    "oidc_client_secret_file": "/run/secrets/obsl",
                },
                "not both",
            ),
            (
                {"oidc_client_id": "obsl-app", "oidc_client_secret_file": "/no/such/file"},
                "cannot be read",
            ),
        ],
    )
    def test_half_a_client_refuses_to_start(self, settings: dict[str, str], match: str) -> None:
        with pytest.raises(AuthConfigError, match=match):
            self._client(**settings)

    def test_the_secret_is_not_in_the_settings_repr(self) -> None:
        assert "s3cret-value" not in repr(Settings(oidc_client_secret="s3cret-value"))


class TestFlight:
    @pytest.mark.parametrize(
        ("error", "flight_error"),
        [
            ("SignInRequiredError", "FlightUnauthenticatedError"),
            ("SignInInteractionRequiredError", "FlightUnauthenticatedError"),
            ("SignInRefusedError", "FlightUnauthorizedError"),
            ("SignInUnavailableError", "FlightUnavailableError"),
        ],
    )
    def test_a_sign_in_failure_is_the_matching_flight_error(
        self, error: str, flight_error: str
    ) -> None:
        from ob_flight.server import _flight_error
        from pyarrow import flight

        mapped = _flight_error(getattr(user_login, error)("the reason"))
        assert type(mapped) is getattr(flight, flight_error)
        assert "the reason" in str(mapped)


# --- End to end: REST, two signed-in users, a real file cache --------------------

import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from orionbelt.api.deps import (  # noqa: E402
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from tests.conftest import SAMPLE_MODEL_YAML  # noqa: E402

SERVICE_KEY = "obsl_pat_user_login_service_key_0123456789"
MODEL_YAML = (
    SAMPLE_MODEL_YAML + "\nsettings:\n  defaultDialect: databricks\n  defaultDataSource: crm\n"
)
QUERY = {"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}}


class Warehouse:
    """Stands in for Databricks: the token service and the driver."""

    def __init__(self) -> None:
        self.exchanges: list[str] = []
        self.connections: list[str] = []

    def post(self, url: str, data: dict[str, str], timeout: float, auth: Any = None) -> Any:
        self.exchanges.append(data["subject_token"])
        token = f"dbx-{len(self.exchanges)}"
        return SimpleNamespace(
            status_code=200, json=lambda: {"access_token": token, "expires_in": 3600}
        )

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


@pytest.fixture
async def end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from ob_flight import db_router

    for name, value in USER_LOGIN_ENV.items():
        monkeypatch.setenv(name, value)
    warehouse = Warehouse()
    monkeypatch.setattr(user_login.httpx, "post", warehouse.post)
    monkeypatch.setitem(
        sys.modules, "fake_ob_databricks", SimpleNamespace(connect=warehouse.connect)
    )
    monkeypatch.setitem(db_router.VENDOR_MAP, "databricks", "fake_ob_databricks")
    user_login.reset_tokens()

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
        api_keys=SERVICE_KEY,
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, idp, warehouse
    await cache.shutdown()
    reset_session_manager()
    user_login.reset_tokens()
    db_router.close_all_pools()


async def _execute(client: AsyncClient, headers: dict[str, str]) -> Any:
    sid = (await client.post("/v1/sessions", headers=headers)).json()["session_id"]
    loaded = await client.post(
        f"/v1/sessions/{sid}/models", json={"model_yaml": MODEL_YAML}, headers=headers
    )
    assert loaded.status_code == 201, loaded.text
    mid = loaded.json()["model_id"]
    return await client.post(
        f"/v1/sessions/{sid}/query/execute",
        json={"model_id": mid, "query": QUERY},
        headers=headers,
    )


async def test_each_user_queries_as_themselves_and_has_their_own_cache(end_to_end: Any) -> None:
    client, idp, warehouse = end_to_end
    alice = {"Authorization": f"Bearer {idp.token('alice', scope='obsl:write')}"}
    bob = {"Authorization": f"Bearer {idp.token('bob', scope='obsl:write')}"}

    first = await _execute(client, alice)
    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False
    assert first.json()["ttl_source"] == "user_login_capped"  # 3600 capped to 300
    again = await _execute(client, alice)
    assert again.json()["cached"] is True  # Alice's own entry

    bobs = await _execute(client, bob)
    assert bobs.status_code == 200, bobs.text
    assert bobs.json()["cached"] is False  # never Alice's rows

    assert warehouse.exchanges == [alice["Authorization"][7:], bob["Authorization"][7:]]
    assert warehouse.connections == ["dbx-1", "dbx-2"]


async def test_a_service_key_is_refused_before_the_cache(end_to_end: Any) -> None:
    client, idp, warehouse = end_to_end
    alice = {"Authorization": f"Bearer {idp.token('alice', scope='obsl:write')}"}
    assert (await _execute(client, alice)).status_code == 200  # an entry now exists

    refused = await _execute(client, {"X-API-Key": SERVICE_KEY})
    assert refused.status_code == 401
    assert refused.json()["detail"]["code"] == "SIGN_IN_REQUIRED"
    assert warehouse.connections == ["dbx-1"]
