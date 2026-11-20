"""The caller of the current request, and what it may use.

A surface adapter binds the authenticated :class:`Principal` for the
duration of a request (REST does it in ``api/auth.py``); code below it,
such as the session manager, reads it back with :func:`current_principal`
instead of having it threaded through every call. ContextVars are isolated
per async task and copied into worker threads, so concurrent requests never
see each other's caller.

When nothing is bound (startup, background jobs, and the Flight and pgwire
surfaces until they accept user identities), :func:`current_principal`
returns ``None`` and :func:`may_use` allows everything, as before ownership
existed. See design/PLAN_authentication.md §6, Phase 4a.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from orionbelt.auth.principal import SCOPE_ADMIN, Principal

_current_principal: ContextVar[Principal | None] = ContextVar(
    "orionbelt_current_principal", default=None
)


def bind_principal(principal: Principal) -> Token[Principal | None]:
    """Bind ``principal`` as the caller of the current request."""
    return _current_principal.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    """Undo a :func:`bind_principal` binding."""
    _current_principal.reset(token)


def current_principal() -> Principal | None:
    """Return the caller of the current request, or ``None`` when none is bound."""
    return _current_principal.get()


def may_use(principal: Principal | None, owner: str | None) -> bool:
    """Return True when ``principal`` may use something ``owner`` created.

    Something without an owner (created by an API key, by ``AUTH_MODE=none``,
    or by the server itself) is shared. Something with an owner is visible
    to that owner and to admins. Without a bound caller, everything is.
    """
    if owner is None or principal is None:
        return True
    return principal.sub == owner or principal.has_scope(SCOPE_ADMIN)
