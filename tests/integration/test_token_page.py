"""The BI tokens page (``/tokens``): when it is served, what it is told, its CSP."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import reset_auth
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

TOKEN_ENDPOINT = "https://login.example.test/oauth2/token"
AUTHORIZATION_ENDPOINT = "https://login.example.test/oauth2/authorize"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(FakeIssuer().jwks()))
    values: dict[str, Any] = {
        "session_ttl_seconds": 3600,
        "session_cleanup_interval": 9999,
        "auth_mode": "oidc",
        "oidc_issuer": ISSUER,
        "oidc_audience": AUDIENCE,
        "oidc_jwks_file": str(jwks),
        "oidc_authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "oidc_token_endpoint": TOKEN_ENDPOINT,
        "oidc_tokens_client_id": "obsl-tokens",
        "oidc_tokens_scopes": "openid obsl:read",
        "token_store_path": str(tmp_path / "bi.sqlite"),
        "token_default_days": 30,
        "token_max_days": 180,
    }
    return Settings(**{**values, **overrides})


@asynccontextmanager
async def _client(settings: Settings, root_path: str = "") -> AsyncIterator[AsyncClient]:
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode=settings.auth_mode,
        oidc=oidc_config(settings),
    )
    try:
        transport = ASGITransport(app=app, root_path=root_path)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        reset_session_manager()
        reset_auth()


def _config(page: str) -> dict[str, Any]:
    found = re.search(r'<script type="application/json" id="config">(.*?)</script>', page, re.S)
    assert found is not None
    return dict(json.loads(found.group(1)))


async def test_the_page_is_told_how_to_sign_in(tmp_path: Path) -> None:
    async with _client(_settings(tmp_path)) as client:
        response = await client.get("/tokens")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert _config(response.text) == {
        "client_id": "obsl-tokens",
        "scopes": "openid obsl:read",
        "audience": AUDIENCE,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "default_days": 30,
        "max_days": 180,
    }


async def test_its_policy_allows_its_own_files_and_the_token_endpoint(tmp_path: Path) -> None:
    async with _client(_settings(tmp_path)) as client:
        for path, media_type in (
            ("/tokens", "text/html"),
            ("/tokens/app.js", "text/javascript"),
            ("/tokens/app.css", "text/css"),
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith(media_type)
            policy = response.headers["content-security-policy"]
            assert policy.startswith("default-src 'self'; connect-src 'self' ")
            assert "https://login.example.test" in policy
            assert "unsafe-inline" not in policy  # no inline script runs on the page


async def test_under_a_root_path_its_policy_still_allows_the_token_endpoint(
    tmp_path: Path,
) -> None:
    """Mounted under ``/obsl``: ``/obsl/tokens`` is the page, with the page's policy."""
    async with _client(_settings(tmp_path), root_path="/obsl") as client:
        for path in ("/obsl/tokens", "/obsl/tokens/app.js", "/obsl/tokens/app.css"):
            response = await client.get(path)
            assert response.status_code == 200, path
            policy = response.headers["content-security-policy"]
            assert policy.startswith("default-src 'self'; connect-src 'self' "), path
            assert "https://login.example.test" in policy, path
        docs = await client.get("/obsl/docs")
        assert "https://cdn.jsdelivr.net" in docs.headers["content-security-policy"]


async def test_a_setting_cannot_end_the_config_script_early(tmp_path: Path) -> None:
    scopes = "openid </script><script>alert(1)</script>"
    async with _client(_settings(tmp_path, oidc_tokens_scopes=scopes)) as client:
        page = (await client.get("/tokens")).text
    assert page.count("</script>") == 2  # the config block's and app.js's own
    assert _config(page)["scopes"] == scopes


@pytest.mark.parametrize(
    "change",
    [
        {"oidc_tokens_client_id": ""},  # no client of its own
        {"token_store_path": ""},  # no token store
    ],
)
async def test_without_its_client_or_a_store_there_is_no_page(
    tmp_path: Path, change: dict[str, str]
) -> None:
    async with _client(_settings(tmp_path, **change)) as client:
        for path in ("/tokens", "/tokens/app.js", "/tokens/app.css"):
            assert (await client.get(path)).status_code == 404, path


async def test_without_oidc_there_is_no_page(tmp_path: Path) -> None:
    async with _client(Settings(oidc_tokens_client_id="obsl-tokens")) as client:
        assert (await client.get("/tokens")).status_code == 404


async def test_it_is_not_part_of_the_api_document(tmp_path: Path) -> None:
    async with _client(_settings(tmp_path)) as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    assert not [path for path in paths if path.startswith("/tokens")]
