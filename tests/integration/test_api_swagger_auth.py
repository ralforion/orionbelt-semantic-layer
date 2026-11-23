"""The OpenAPI document declares the auth that is on, so Swagger UI can send it."""

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
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

API_KEY = "obsl_pat_swagger_test_key_0123456789abcdef"


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


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_session_manager()


@pytest.fixture
def jwks_file(tmp_path: Path) -> str:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(FakeIssuer().jwks()))
    return str(path)


async def _schema(app: FastAPI) -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/openapi.json")
        assert resp.status_code == 200
        return dict(resp.json())


def _security(schema: dict[str, Any], path: str, method: str = "get") -> Any:
    return schema["paths"][path][method].get("security")


async def test_no_auth_declares_nothing() -> None:
    schema = await _schema(_app())
    assert "securitySchemes" not in schema.get("components", {})
    assert _security(schema, "/v1/dialects") is None


class TestApiKey:
    async def test_every_v1_operation_takes_the_key(self) -> None:
        schema = await _schema(
            _app(auth_mode="api_key", api_keys=API_KEY, api_key_header="X-Obsl-Key")
        )
        assert schema["components"]["securitySchemes"] == {
            "ApiKey": {"type": "apiKey", "in": "header", "name": "X-Obsl-Key"}
        }
        assert _security(schema, "/v1/dialects") == [{"ApiKey": []}]
        assert _security(schema, "/v1/sessions", "post") == [{"ApiKey": []}]

    async def test_unauthenticated_routes_declare_nothing(self) -> None:
        schema = await _schema(_app(auth_mode="api_key", api_keys=API_KEY))
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

    async def test_a_token_is_pasted(self, jwks_file: str) -> None:
        schema = await _schema(self._oidc(jwks_file))
        assert schema["components"]["securitySchemes"] == {
            "BearerToken": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "An access token from the identity provider.",
            }
        }
        assert _security(schema, "/v1/dialects") == [{"BearerToken": []}]

    async def test_or_an_api_key_when_there_are_service_keys(self, jwks_file: str) -> None:
        schema = await _schema(self._oidc(jwks_file, api_keys=API_KEY))
        assert list(schema["components"]["securitySchemes"]) == ["BearerToken", "ApiKey"]
        assert _security(schema, "/v1/dialects") == [{"BearerToken": []}, {"ApiKey": []}]
