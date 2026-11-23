"""The OpenAPI document and Swagger UI (/docs) describe the auth that is on."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import init_auth
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

API_KEY = "obsl_pat_swagger_test_key_0123456789abcdef"
AUTHORIZE = "https://idp.example/authorize"
TOKEN = "https://tokens.example/token"


def _app(**overrides: Any) -> FastAPI:
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999, **overrides)
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode=settings.auth_mode,
        api_keys=settings.api_keys,
        oidc=oidc_config(settings),
    )
    return app


@pytest.fixture
def jwks_file(tmp_path: Path) -> str:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(FakeIssuer().jwks()))
    return str(path)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_session_manager()


async def _get(app: FastAPI, path: str) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path)


def _security(schema: dict[str, Any], path: str, method: str = "get") -> Any:
    return schema["paths"][path][method].get("security")


class TestNoAuth:
    async def test_the_document_declares_nothing(self) -> None:
        schema = (await _get(_app(), "/openapi.json")).json()
        assert "securitySchemes" not in schema.get("components", {})
        assert _security(schema, "/v1/dialects") is None


class TestApiKey:
    async def test_every_v1_operation_takes_the_key(self) -> None:
        app = _app(auth_mode="api_key", api_keys=API_KEY, api_key_header="X-Obsl-Key")
        schema = (await _get(app, "/openapi.json")).json()
        assert schema["components"]["securitySchemes"] == {
            "ApiKey": {"type": "apiKey", "in": "header", "name": "X-Obsl-Key"}
        }
        assert _security(schema, "/v1/dialects") == [{"ApiKey": []}]
        assert _security(schema, "/v1/sessions", "post") == [{"ApiKey": []}]

    async def test_unauthenticated_routes_declare_nothing(self) -> None:
        schema = (await _get(_app(auth_mode="api_key", api_keys=API_KEY), "/openapi.json")).json()
        assert _security(schema, "/health") is None
        assert _security(schema, "/v1/heartbeat", "post") is None  # its own token


class TestOidc:
    def _oidc(self, jwks_file: str, **overrides: Any) -> FastAPI:
        values: dict[str, Any] = {
            "auth_mode": "oidc",
            "oidc_issuer": ISSUER,
            "oidc_audience": AUDIENCE,
            "oidc_jwks_file": jwks_file,
        }
        values.update(overrides)
        return _app(**values)

    async def test_sign_in_scheme_with_explicit_endpoints(self, jwks_file: str) -> None:
        app = self._oidc(
            jwks_file,
            api_keys=API_KEY,
            oidc_swagger_client_id="obsl-swagger",
            oidc_swagger_scopes="openid obsl:read",
            oidc_authorization_endpoint=AUTHORIZE,
            oidc_token_endpoint=TOKEN,
        )
        schemes = (await _get(app, "/openapi.json")).json()["components"]["securitySchemes"]
        assert list(schemes) == ["OpenIdConnect", "BearerToken", "ApiKey"]
        assert schemes["OpenIdConnect"]["flows"] == {
            "authorizationCode": {
                "authorizationUrl": AUTHORIZE,
                "tokenUrl": TOKEN,
                "scopes": {"openid": "openid", "obsl:read": "obsl:read"},
            }
        }

    async def test_swagger_ui_signs_in_with_pkce(self, jwks_file: str) -> None:
        app = self._oidc(
            jwks_file,
            oidc_swagger_client_id="obsl-swagger",
            oidc_authorization_endpoint=AUTHORIZE,
            oidc_token_endpoint=TOKEN,
        )
        html = (await _get(app, "/docs")).text
        assert '"clientId": "obsl-swagger"' in html
        assert '"usePkceWithAuthorizationCodeGrant": true' in html
        assert f'"audience": "{AUDIENCE}"' in html

    async def test_csp_lets_docs_reach_the_token_endpoint(self, jwks_file: str) -> None:
        app = self._oidc(
            jwks_file,
            oidc_swagger_client_id="obsl-swagger",
            oidc_authorization_endpoint=AUTHORIZE,
            oidc_token_endpoint=TOKEN,
        )
        csp = (await _get(app, "/docs")).headers["content-security-policy"]
        assert "connect-src 'self' https://idp.test https://tokens.example;" in csp
        redirect = (await _get(app, "/docs/oauth2-redirect")).headers["content-security-policy"]
        assert "script-src 'self' 'unsafe-inline'" in redirect  # its inline script runs

    async def test_endpoints_from_discovery(self) -> None:
        # The keys came through discovery, so its endpoints are known.
        idp = FakeIssuer()
        settings_kwargs = {
            "auth_mode": "oidc",
            "oidc_issuer": ISSUER,
            "oidc_audience": AUDIENCE,
            "oidc_swagger_client_id": "obsl-swagger",
        }
        app = create_app(settings=Settings(**settings_kwargs))
        init_session_manager(SessionManager(ttl_seconds=3600, cleanup_interval=9999))
        config = oidc_config(Settings(**settings_kwargs))
        assert config is not None
        init_auth(
            auth_mode="oidc", oidc=config, verifier=JwtVerifier(config, fetch=CountingFetch(idp))
        )
        flow = (await _get(app, "/openapi.json")).json()["components"]["securitySchemes"][
            "OpenIdConnect"
        ]["flows"]["authorizationCode"]
        assert flow["authorizationUrl"] == idp.discovery()["authorization_endpoint"]
        assert flow["tokenUrl"] == idp.discovery()["token_endpoint"]

    async def test_without_a_client_a_token_or_key_is_pasted(self, jwks_file: str) -> None:
        app = self._oidc(jwks_file)
        schema = (await _get(app, "/openapi.json")).json()
        assert list(schema["components"]["securitySchemes"]) == ["BearerToken"]
        assert _security(schema, "/v1/dialects") == [{"BearerToken": []}]
        assert "initOAuth" not in (await _get(app, "/docs")).text

    async def test_a_client_without_endpoints_offers_no_sign_in_and_says_so(
        self, jwks_file: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = self._oidc(jwks_file, oidc_swagger_client_id="obsl-swagger")
        schemes = (await _get(app, "/openapi.json")).json()["components"]["securitySchemes"]
        assert "OpenIdConnect" not in schemes
        assert "endpoints are unknown" in caplog.text
