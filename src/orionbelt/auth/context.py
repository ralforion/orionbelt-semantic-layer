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

import hashlib
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
# The caller's key (:func:`caller_key`), for records a caller reads back as
# its own, such as the gap log.
_current_caller: ContextVar[str | None] = ContextVar("orionbelt_current_caller", default=None)


@dataclass(frozen=True)
class Binding:
    """What :func:`bind_principal` set, for :func:`reset_principal` to undo."""

    principal: Token[Principal | None]
    credential: Token[str | None]
    caller: Token[str | None]


def caller_key(principal: Principal, session_id: str | None = None) -> str | None:
    """A short key for who is calling, from which the identity cannot be read back.

    A signed-in user (OIDC or BI token) is known by ``sub``, an API key caller
    by the key's id. A caller with neither (``AUTH_MODE=none``) is known by the
    session its request names, and without one there is no key. The key is
    twelve hex characters of a SHA-256, so no user name, key id or session id
    is kept where the key is.
    """
    if principal.kind == "user" and principal.sub:
        identity = f"user:{principal.sub}"
    elif principal.kind == "api_key" and principal.key_id:
        identity = f"key:{principal.key_id}"
    elif session_id:
        identity = f"session:{session_id}"
    else:
        return None
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def bind_principal(
    principal: Principal, credential: str | None = None, *, session_id: str | None = None
) -> Binding:
    """Bind ``principal`` as the caller of the current request.

    ``credential`` is the identity provider token a signed-in user called
    with; None for an API key or no auth. ``session_id`` is the session the
    request names, which stands in for a caller without an identity.
    """
    return Binding(
        _current_principal.set(principal),
        _current_credential.set(credential),
        _current_caller.set(caller_key(principal, session_id)),
    )


def reset_principal(binding: Binding) -> None:
    """Undo a :func:`bind_principal` binding."""
    _current_caller.reset(binding.caller)
    _current_credential.reset(binding.credential)
    _current_principal.reset(binding.principal)


def current_principal() -> Principal | None:
    """Return the caller of the current request, or ``None`` when none is bound."""
    return _current_principal.get()


def current_credential() -> str | None:
    """The signed-in user's token for the current request, or None."""
    return _current_credential.get()


def current_caller() -> str | None:
    """The current caller's key (:func:`caller_key`), or None when it has none."""
    return _current_caller.get()


def may_use(principal: Principal | None, owner: str | None) -> bool:
    """Return True when ``principal`` may use something ``owner`` created.

    Something without an owner (created by an API key, by ``AUTH_MODE=none``,
    or by the server itself) is shared. Something with an owner is visible
    to that owner and to admins. Without a bound caller, everything is.
    """
    if owner is None or principal is None:
        return True
    return principal.sub == owner or principal.has_scope(SCOPE_ADMIN)
