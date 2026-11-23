"""How the OpenAPI document and Swagger UI (/docs) describe and do authentication.

With auth on, every ``/v1`` operation declares the credentials it accepts, so
Swagger's "Authorize" button appears and sends them:

* ``AUTH_MODE=api_key``: an API key in the configured header.
* ``AUTH_MODE=oidc``: a bearer token pasted in; an API key when ``API_KEYS``
  is set; and, with ``OIDC_SWAGGER_CLIENT_ID``, sign-in at the IdP
  (authorization code + PKCE), after which Swagger sends the user's token.

``AUTH_MODE=none`` leaves the document as it was. See
docs/guide/authentication.md.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI

from orionbelt.auth import MODE_API_KEY, MODE_NONE, MODE_OIDC, get_oidc_metadata, resolve_mode
from orionbelt.settings import Settings

logger = logging.getLogger("orionbelt.api")

#: Operations under /v1 that authenticate on their own (``HEARTBEAT_AUTH_TOKEN``).
_OWN_AUTH_PATHS = frozenset({"/v1/heartbeat"})


def _swagger_sign_in(settings: Settings) -> bool:
    return resolve_mode(settings.auth_mode) == MODE_OIDC and bool(
        settings.oidc_swagger_client_id.strip()
    )


def swagger_init_oauth(settings: Settings) -> dict[str, Any] | None:
    """Swagger UI's ``initOAuth`` settings when /docs signs in with the IdP, else None.

    ``audience`` is sent because some IdPs (Auth0) issue an access token for
    the API only when asked for it; the others ignore an unknown parameter
    (RFC 6749 §3.1).
    """
    if not _swagger_sign_in(settings):
        return None
    return {
        "clientId": settings.oidc_swagger_client_id.strip(),
        "usePkceWithAuthorizationCodeGrant": True,
        "scopes": settings.oidc_swagger_scopes.strip(),
        "additionalQueryStringParams": {"audience": settings.oidc_audience.strip()},
    }


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def docs_connect_sources(settings: Settings) -> tuple[str, ...]:
    """Origins /docs must be allowed to fetch: the issuer's and the token endpoint's.

    Swagger UI exchanges the code for a token from the browser, at the token
    endpoint the OpenAPI document advertises: ``OIDC_TOKEN_ENDPOINT``, else the
    one discovery found, which may be on another host than the issuer. Read
    per request, because discovery happens at startup, after the app is built.
    """
    if not _swagger_sign_in(settings):
        return ()
    token_url = _endpoint(settings.oidc_token_endpoint, "token_endpoint") or ""
    urls = (settings.oidc_issuer.strip(), token_url)
    return tuple(dict.fromkeys(o for o in (_origin(u) for u in urls if u) if o))


def _endpoint(explicit: str, discovered: str) -> str | None:
    """An IdP endpoint: the explicit setting, else the discovery document's ``discovered``."""
    if explicit.strip():
        return explicit.strip()
    value = (get_oidc_metadata() or {}).get(discovered)
    return value if isinstance(value, str) and value else None


def _sign_in_scheme(settings: Settings) -> dict[str, Any] | None:
    """The authorization-code scheme for /docs sign-in; None when the endpoints are unknown."""
    authorization_url = _endpoint(settings.oidc_authorization_endpoint, "authorization_endpoint")
    token_url = _endpoint(settings.oidc_token_endpoint, "token_endpoint")
    if not (authorization_url and token_url):
        logger.warning(
            "OIDC_SWAGGER_CLIENT_ID is set, but the IdP's authorization and token "
            "endpoints are unknown: /docs offers no sign-in. Set OIDC_AUTHORIZATION_ENDPOINT "
            "and OIDC_TOKEN_ENDPOINT, or leave OIDC_JWKS_URI / OIDC_JWKS_FILE unset so the "
            "discovery document is read."
        )
        return None
    scopes = settings.oidc_swagger_scopes.split()
    return {
        "type": "oauth2",
        "description": "Sign in with the identity provider.",
        "flows": {
            "authorizationCode": {
                "authorizationUrl": authorization_url,
                "tokenUrl": token_url,
                "scopes": {scope: scope for scope in scopes},
            }
        },
    }


def security_schemes(settings: Settings) -> dict[str, dict[str, Any]]:
    """The OpenAPI security schemes for the configured auth mode ({} without auth)."""
    mode = resolve_mode(settings.auth_mode)
    if mode == MODE_NONE:
        return {}
    api_key = {"type": "apiKey", "in": "header", "name": settings.api_key_header}
    if mode == MODE_API_KEY:
        return {"ApiKey": api_key}
    schemes: dict[str, dict[str, Any]] = {}
    if _swagger_sign_in(settings):
        sign_in = _sign_in_scheme(settings)
        if sign_in is not None:
            schemes["OpenIdConnect"] = sign_in
    schemes["BearerToken"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "An access token from the identity provider.",
    }
    if settings.api_keys.strip():
        schemes["ApiKey"] = api_key
    return schemes


def install_openapi_security(app: FastAPI, settings: Settings) -> None:
    """Make ``app.openapi()`` declare the security schemes on every /v1 operation.

    Built on first use, after startup has read the issuer's discovery
    document. Any one scheme is enough (OpenAPI lists alternatives).
    """
    if resolve_mode(settings.auth_mode) == MODE_NONE:
        return
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = generate()
        schemes = security_schemes(settings)
        schema.setdefault("components", {})["securitySchemes"] = schemes
        requirement: list[dict[str, list[str]]] = [{name: []} for name in schemes]
        for path, item in schema.get("paths", {}).items():
            if not path.startswith("/v1/") or path in _OWN_AUTH_PATHS:
                continue
            for operation in item.values():
                if isinstance(operation, dict):
                    operation["security"] = requirement
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
