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
from dataclasses import dataclass

from orionbelt.auth.principal import SCOPE_ADMIN, Principal

_current_principal: ContextVar[Principal | None] = ContextVar(
    "orionbelt_current_principal", default=None
)
# The token a signed-in user called with, so a data source that logs in as
# the user can exchange it. Never logged, cached or stored.
_current_credential: ContextVar[str | None] = ContextVar(
    "orionbelt_current_credential", default=None
)


@dataclass(frozen=True)
class Binding:
    """What :func:`bind_principal` set, for :func:`reset_principal` to undo."""

    principal: Token[Principal | None]
    credential: Token[str | None]


def bind_principal(principal: Principal, credential: str | None = None) -> Binding:
    """Bind ``principal`` as the caller of the current request.

    ``credential`` is the identity provider token a signed-in user called
    with; None for an API key or no auth.
    """
    return Binding(_current_principal.set(principal), _current_credential.set(credential))


def reset_principal(binding: Binding) -> None:
    """Undo a :func:`bind_principal` binding."""
    _current_credential.reset(binding.credential)
    _current_principal.reset(binding.principal)


def current_principal() -> Principal | None:
    """Return the caller of the current request, or ``None`` when none is bound."""
    return _current_principal.get()


def current_credential() -> str | None:
    """The signed-in user's token for the current request, or None."""
    return _current_credential.get()


def may_use(principal: Principal | None, owner: str | None) -> bool:
    """Return True when ``principal`` may use something ``owner`` created.

    Something without an owner (created by an API key, by ``AUTH_MODE=none``,
    or by the server itself) is shared. Something with an owner is visible
    to that owner and to admins. Without a bound caller, everything is.
    """
    if owner is None or principal is None:
        return True
    return principal.sub == owner or principal.has_scope(SCOPE_ADMIN)
