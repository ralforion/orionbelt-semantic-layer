"""REST adapter for the shared auth subsystem.

Reads the credential from the configured header (or ``Authorization:
Bearer`` fallback), calls :func:`orionbelt.auth.authenticate`, checks the
scope the matched endpoint requires (``api/route_scopes.py``), and binds
the caller for the rest of the request so the session manager can tell
whose sessions it may show. Wired as a router-level dependency on ``/v1``
in ``api/app.py``. See design/PLAN_authentication.md §3.1 and §5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request

from orionbelt import auth as auth_core
from orionbelt.api.route_scopes import required_scope
from orionbelt.auth import Principal


def _extract_credential(request: Request) -> str | None:
    """Pull the credential from the configured header or a Bearer token."""
    header_value = request.headers.get(auth_core.get_header_name())
    if header_value:
        return header_value.strip() or None
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip() or None
    return None


def _authenticate(request: Request) -> Principal:
    """Authenticate the request or raise 401 (no credential) / 403 (wrong one)."""
    credential = _extract_credential(request)
    try:
        return auth_core.authenticate(credential)
    except auth_core.AuthRequiredError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": f'ApiKey header="{auth_core.get_header_name()}"'},
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
    :func:`orionbelt.auth.bind_principal` until the request ends.
    """
    principal = _authenticate(request)
    scope = required_scope(request.scope.get("endpoint"))
    if not principal.has_scope(scope):
        raise HTTPException(
            status_code=403,
            detail={
                "code": auth_core.AUTH_INSUFFICIENT_SCOPE,
                "message": f"Insufficient scope: requires {scope}",
            },
        )
    request.state.principal = principal
    token = auth_core.bind_principal(principal)
    try:
        yield principal
    finally:
        auth_core.reset_principal(token)
