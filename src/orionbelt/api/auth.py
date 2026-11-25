"""REST adapter for the shared auth subsystem.

Reads the credential from the configured header (or ``Authorization:
Bearer`` fallback), calls :func:`orionbelt.auth.authenticate`, checks the
scope the matched endpoint requires (``api/route_scopes.py``), and binds
the caller for the rest of the request so the session manager can tell
whose sessions it may show. Wired as a router-level dependency on ``/v1``
in ``api/app.py``. See docs/guide/authentication.md.

Under ``AUTH_MODE=oidc`` a missing or bad token is answered as RFC 6750
says: 401 with ``WWW-Authenticate: Bearer ... error="invalid_token"``, and
403 with ``error="insufficient_scope"``. A wrong API key stays 403.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from orionbelt import auth as auth_core
from orionbelt.api.route_scopes import required_scope
from orionbelt.auth import Principal

_REALM = 'realm="obsl"'


def _extract_credential(request: Request) -> str | None:
    """Pull the credential from the configured header or a Bearer token."""
    header_value = request.headers.get(auth_core.get_header_name())
    if header_value:
        return header_value.strip() or None
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip() or None
    return None


def _challenge() -> str:
    """The ``WWW-Authenticate`` value for a request without a credential."""
    if auth_core.get_mode() == auth_core.MODE_OIDC:
        return f"Bearer {_REALM}"
    return f'ApiKey header="{auth_core.get_header_name()}"'


async def _authenticate(request: Request) -> Principal:
    """Authenticate the request or raise 401 / 403."""
    credential = _extract_credential(request)
    try:
        if auth_core.get_mode() == auth_core.MODE_OIDC:
            # Verifying a token may fetch the issuer's keys: keep it off the loop.
            return await run_in_threadpool(auth_core.authenticate, credential)
        return auth_core.authenticate(credential)
    except auth_core.AuthRequiredError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": _challenge()},
        ) from exc
    except auth_core.TokenInvalidError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": exc.code, "message": exc.message},
            headers={
                "WWW-Authenticate": (
                    f'Bearer {_REALM}, error="invalid_token", error_description="{exc.message}"'
                )
            },
        ) from exc
    except auth_core.AuthInvalidError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


async def require_auth(request: Request) -> AsyncIterator[Principal]:
    """FastAPI dependency on ``/v1``: authenticate, authorize, bind the caller.

    Raises 401/403 when the credential is missing or wrong, and 403 when the
    caller lacks the scope the endpoint requires. The principal is stored on
    ``request.state.principal`` and bound with
    :func:`orionbelt.auth.bind_principal` until the request ends; its ``sub``
    is added to the request's log lines.
    """
    try:
        principal = await _authenticate(request)
    except HTTPException as exc:
        detail = exc.detail
        request.state.audit_reason = (
            detail.get("code") if isinstance(detail, dict) else auth_core.AUTH_REQUIRED
        )
        raise
    scope = required_scope(request.scope.get("endpoint"))
    if not principal.has_scope(scope):
        request.state.principal = principal  # who was refused, for the audit log
        request.state.audit_reason = auth_core.AUTH_INSUFFICIENT_SCOPE
        headers = None
        if auth_core.get_mode() == auth_core.MODE_OIDC:
            headers = {
                "WWW-Authenticate": f'Bearer {_REALM}, error="insufficient_scope", scope="{scope}"'
            }
        raise HTTPException(
            status_code=403,
            detail={
                "code": auth_core.AUTH_INSUFFICIENT_SCOPE,
                "message": f"Insufficient scope: requires {scope}",
            },
            headers=headers,
        )
    request.state.principal = principal
    # A signed-in user's token is kept for the request, for a data source that
    # logs in as the user; an API key is not, and neither is a BI token: it is
    # no identity provider token, so there is nothing to exchange.
    credential = (
        _extract_credential(request)
        if principal.kind == "user" and principal.token_id is None
        else None
    )
    token = auth_core.bind_principal(principal, credential)
    try:
        if principal.sub is None:
            yield principal
        else:
            with structlog.contextvars.bound_contextvars(sub=principal.sub):
                yield principal
    finally:
        auth_core.reset_principal(token)
