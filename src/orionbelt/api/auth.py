"""REST adapter for the shared auth subsystem.

Reads the credential from the configured header (or ``Authorization:
Bearer`` fallback), calls :func:`orionbelt.auth.authenticate`, and
translates the result into FastAPI accept/reject. Wired as a router-level
dependency on ``/v1`` in ``api/app.py``. See design/PLAN_authentication.md §3.1.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from orionbelt import auth as auth_core
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


async def require_auth(request: Request) -> Principal:
    """FastAPI dependency: authenticate the request or raise 401/403.

    Stores the resolved principal on ``request.state.principal`` for
    downstream handlers / logging.
    """
    credential = _extract_credential(request)
    try:
        principal = auth_core.authenticate(credential)
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

    request.state.principal = principal
    return principal


def require_scope(scope: str) -> Callable[[Request], Awaitable[Principal]]:
    """Build a dependency that requires ``scope`` in addition to authentication.

    No-op in ``none`` / ``api_key`` modes (those principals carry full
    scopes); enforced for OIDC user principals in Phase 4.
    """

    async def _checker(request: Request) -> Principal:
        principal = await require_auth(request)
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": auth_core.AUTH_INSUFFICIENT_SCOPE,
                    "message": f"Insufficient scope: requires {scope}",
                },
            )
        return principal

    return _checker
