"""The BI tokens page (``/tokens``): sign in, then create, list and revoke tokens.

A small page served by the API, outside ``/v1``. It signs in at the identity
provider in the browser (authorization code with PKCE) as its own public
client, ``OIDC_TOKENS_CLIENT_ID``, separate from Swagger's, and calls
``/v1/auth/tokens`` with that sign-in. Served only under ``AUTH_MODE=oidc``
with a token store and that client; otherwise every path answers 404. See
docs/guide/bi-tokens.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import HTMLResponse

from orionbelt.auth import (
    MODE_OIDC,
    get_authorization_endpoint,
    get_token_endpoint,
    get_token_store,
    resolve_mode,
)
from orionbelt.settings import Settings

_FILES = Path(__file__).parent


def token_page_enabled(settings: Settings) -> bool:
    """Whether ``/tokens`` is served: ``oidc``, its own client, and a token store."""
    return (
        resolve_mode(settings.auth_mode) == MODE_OIDC
        and bool(settings.oidc_tokens_client_id.strip())
        and bool(settings.token_store_path.strip())
    )


def _script_json(value: object) -> str:
    """JSON safe inside a ``<script>`` element: no ``<`` to end it early."""
    return json.dumps(value).replace("<", "\\u003c")


def build_router(settings: Settings) -> APIRouter:
    """The page and its two files, outside the OpenAPI document."""
    router = APIRouter(include_in_schema=False)

    def _require_enabled() -> None:
        if not token_page_enabled(settings) or get_token_store() is None:
            raise HTTPException(status_code=404, detail="Not Found")

    @router.get("/tokens", response_class=HTMLResponse)
    def token_page() -> HTMLResponse:
        store = get_token_store()
        if store is None or not token_page_enabled(settings):
            raise HTTPException(status_code=404, detail="Not Found")
        config = {
            "client_id": settings.oidc_tokens_client_id.strip(),
            "scopes": settings.oidc_tokens_scopes.strip(),
            "audience": settings.oidc_audience.strip(),
            # Read per request: discovery happens at startup, after the app is built.
            "authorization_endpoint": get_authorization_endpoint() or "",
            "token_endpoint": get_token_endpoint() or "",
            "default_days": store.default_days,
            "max_days": store.max_days,
        }
        page = (_FILES / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            page.replace("__CONFIG__", _script_json(config)),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/tokens/app.js")
    def token_page_script() -> Response:
        _require_enabled()
        return Response((_FILES / "app.js").read_bytes(), media_type="text/javascript")

    @router.get("/tokens/app.css")
    def token_page_style() -> Response:
        _require_enabled()
        return Response((_FILES / "app.css").read_bytes(), media_type="text/css")

    return router
