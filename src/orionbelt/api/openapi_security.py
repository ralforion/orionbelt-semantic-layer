"""How the OpenAPI document declares authentication, so Swagger UI can send it.

With auth on, every ``/v1`` operation declares the credentials it accepts,
so the "Authorize" button on ``/docs`` appears and Swagger sends what is
pasted there:

* ``AUTH_MODE=api_key``: an API key in the configured header.
* ``AUTH_MODE=oidc``: an access token (``Authorization: Bearer``), and an API
  key when ``API_KEYS`` is set.

``AUTH_MODE=none`` leaves the document as it was. See
docs/guide/authentication.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from orionbelt.auth import MODE_API_KEY, MODE_NONE, resolve_mode
from orionbelt.settings import Settings

#: Operations under /v1 that authenticate on their own (``HEARTBEAT_AUTH_TOKEN``).
_OWN_AUTH_PATHS = frozenset({"/v1/heartbeat"})


def security_schemes(settings: Settings) -> dict[str, dict[str, Any]]:
    """The OpenAPI security schemes for the configured auth mode ({} without auth)."""
    mode = resolve_mode(settings.auth_mode)
    if mode == MODE_NONE:
        return {}
    api_key = {"type": "apiKey", "in": "header", "name": settings.api_key_header}
    if mode == MODE_API_KEY:
        return {"ApiKey": api_key}
    schemes: dict[str, dict[str, Any]] = {
        "BearerToken": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "An access token from the identity provider.",
        }
    }
    if settings.api_keys.strip():
        schemes["ApiKey"] = api_key
    return schemes


def install_openapi_security(app: FastAPI, settings: Settings) -> None:
    """Make ``app.openapi()`` declare the security schemes on every /v1 operation.

    Any one scheme is enough (OpenAPI lists alternatives).
    """
    schemes = security_schemes(settings)
    if not schemes:
        return
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = generate()
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
