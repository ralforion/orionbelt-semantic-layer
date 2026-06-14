"""Authentication seam for the Postgres wire surface.

pgwire defers to the shared auth subsystem (``orionbelt.auth``) so a single
``AUTH_MODE`` governs every surface. The global mode (not the legacy
``PGWIRE_AUTH_MODE`` setting) drives behaviour:

* ``none``    → trust: accept every StartupMessage, no password exchange.
* ``api_key`` → the server requires a credential. The mechanism is chosen by
                :func:`select_mechanism`: SCRAM-SHA-256 by default (never sends
                the key on the wire), or cleartext password when the operator
                sets ``PGWIRE_AUTH_MODE=password``. Either way the "password"
                is the OBSL API key and the ``user`` field is ignored for
                validation (stored only for logging).
* ``oidc``    → Phase 4 (unreachable — ``init_auth`` refuses to start).

Cleartext sends the API key in plaintext on the wire, so untrusted networks
must terminate TLS in front of pgwire (reverse proxy / platform LB). SCRAM
avoids transmitting the raw key and is therefore the default. See
design/PLAN_authentication.md §3.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from orionbelt.auth import (
    MODE_API_KEY,
    MODE_NONE,
    get_api_keys,
    get_mode,
    validate_credential,
)
from orionbelt.pgwire.protocol import StartupMessage

# Password mechanisms offered when a credential is required.
MECH_SCRAM = "scram-sha-256"
MECH_CLEARTEXT = "cleartext"


@dataclass(frozen=True)
class AuthResult:
    """Outcome of an authentication attempt."""

    ok: bool
    error_message: str = ""


def auth_required() -> bool:
    """Return True when the current auth mode needs a credential exchange."""
    return get_mode() == MODE_API_KEY


# Back-compat alias (Phase 2 cleartext-only name).
password_required = auth_required


def select_mechanism(pgwire_auth_mode: str) -> str:
    """Pick the password mechanism for credential exchange.

    SCRAM-SHA-256 is the secure default. Operators opt into cleartext (for
    clients that lack SCRAM support) via ``PGWIRE_AUTH_MODE=password`` or
    ``=cleartext``. Any other value (including the legacy default ``trust``)
    selects SCRAM.
    """
    if (pgwire_auth_mode or "").strip().lower() in {"password", "cleartext"}:
        return MECH_CLEARTEXT
    return MECH_SCRAM


def scram_candidate_keys() -> tuple[str, ...]:
    """Return the configured API keys for SCRAM proof verification."""
    return tuple(get_api_keys())


def authenticate(
    *,
    startup: StartupMessage,
    password: str | None = None,
) -> AuthResult:
    """Validate a connection attempt against the shared auth subsystem.

    Callers must send ``AuthenticationCleartextPassword`` and collect the
    client's ``PasswordMessage`` first whenever :func:`password_required`
    returns True, then pass the password here.
    """
    mode = get_mode()

    if mode == MODE_NONE:
        return AuthResult(ok=True)

    if mode == MODE_API_KEY:
        if not password:
            return AuthResult(ok=False, error_message="password authentication required")
        if validate_credential(password):
            return AuthResult(ok=True)
        return AuthResult(ok=False, error_message="invalid API key")

    # oidc — unreachable in Phase 1/2 (init_auth refuses this mode).
    return AuthResult(ok=False, error_message=f"unsupported auth mode: {mode!r}")
