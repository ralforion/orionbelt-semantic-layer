"""Authentication seam for the Postgres wire surface.

pgwire defers to the shared auth subsystem (``orionbelt.auth``) so a single
``AUTH_MODE`` governs every surface:

* ``none``    → trust: accept every StartupMessage, no password exchange.
* ``api_key`` → the "password" is an API key. The mechanism is chosen by
                :func:`select_mechanism`: SCRAM-SHA-256 by default (never sends
                the key on the wire), or cleartext when the operator sets
                ``PGWIRE_AUTH_MODE=password``.
* ``oidc``    → the password is an API key (as above) or the IdP's access
                token. SCRAM cannot check a token (the server would need to
                hold the secret), so a token is accepted only as a cleartext
                password on a TLS connection; on a TLS connection pgwire
                therefore asks for cleartext. Without TLS only API keys work.

The ``user`` field is ignored for validation (stored only for logging). The
connection is authenticated once, at startup, as Postgres does: it stays open
after the token expires. See docs/guide/authentication.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from orionbelt import auth as auth_core
from orionbelt.auth import (
    MODE_NONE,
    MODE_OIDC,
    SCOPE_READ,
    AuthError,
    Principal,
    get_api_keys,
    get_mode,
    validate_api_key,
)
from orionbelt.auth.jwt_verify import looks_like_jwt
from orionbelt.auth.principal import API_KEY_PRINCIPAL
from orionbelt.pgwire.protocol import StartupMessage

# Password mechanisms offered when a credential is required.
MECH_SCRAM = "scram-sha-256"
MECH_CLEARTEXT = "cleartext"


@dataclass(frozen=True)
class AuthResult:
    """Outcome of an authentication attempt, and the caller when it succeeded."""

    ok: bool
    error_message: str = ""
    principal: Principal | None = None


def auth_required() -> bool:
    """Return True when the current auth mode needs a credential exchange."""
    return get_mode() != MODE_NONE


# Back-compat alias (Phase 2 cleartext-only name).
password_required = auth_required


def select_mechanism(pgwire_auth_mode: str, *, tls: bool = False) -> str:
    """Pick the password mechanism for credential exchange.

    SCRAM-SHA-256 is the secure default. Operators opt into cleartext (for
    clients that lack SCRAM support) via ``PGWIRE_AUTH_MODE=password`` or
    ``=cleartext``. Any other value (including the legacy default ``trust``)
    selects SCRAM. Under ``AUTH_MODE=oidc`` a TLS connection gets cleartext,
    the only mechanism that can carry an access token; TLS protects it.
    """
    if tls and get_mode() == MODE_OIDC:
        return MECH_CLEARTEXT
    if (pgwire_auth_mode or "").strip().lower() in {"password", "cleartext"}:
        return MECH_CLEARTEXT
    return MECH_SCRAM


def scram_candidate_keys() -> tuple[str, ...]:
    """Return the configured API keys for SCRAM proof verification."""
    return tuple(get_api_keys())


def _refused(message: str) -> AuthResult:
    return AuthResult(ok=False, error_message=message)


def authenticate(
    *,
    startup: StartupMessage,
    password: str | None = None,
    tls: bool = False,
) -> AuthResult:
    """Validate a cleartext password against the shared auth subsystem.

    Callers must send ``AuthenticationCleartextPassword`` and collect the
    client's ``PasswordMessage`` first whenever :func:`password_required`
    returns True, then pass the password here, and say whether the
    connection is TLS: an access token is refused on a plain one.
    """
    if get_mode() == MODE_NONE:
        return AuthResult(ok=True)
    if not password:
        return _refused("password authentication required")
    if get_mode() != MODE_OIDC or not looks_like_jwt(password):
        if validate_api_key(password):
            return AuthResult(ok=True, principal=API_KEY_PRINCIPAL)
        return _refused("invalid API key")
    if not tls:
        return _refused(
            "an access token is accepted only over TLS: connect with sslmode=require, "
            "or use an API key"
        )
    try:
        principal = auth_core.authenticate(password)
    except AuthError as exc:
        return _refused(exc.message)
    if not principal.has_scope(SCOPE_READ):
        return _refused(f"insufficient scope: requires {SCOPE_READ}")
    return AuthResult(ok=True, principal=principal)
