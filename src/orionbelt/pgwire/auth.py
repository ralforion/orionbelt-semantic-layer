"""Authentication seam for the Postgres wire surface.

Step 1 supports ``trust`` mode only — the server accepts every
StartupMessage and responds with AuthenticationOk. The function
signature is shaped so the unified-auth keystore (see
design/PLAN_unified_auth.md) drops in at Step 6 without touching
``server.py``:

* ``trust``       → returns ``AuthResult(ok=True)`` immediately.
* ``password``    → server sends AuthenticationCleartextPassword,
                    reads the PasswordMessage, and calls this with the
                    raw password. The keystore validator goes here.
* ``scram-sha-256`` → multi-step SASL exchange — caller drives the
                      handshake and consults this module to compare
                      stored verifiers.
"""

from __future__ import annotations

from dataclasses import dataclass

from orionbelt.pgwire.protocol import StartupMessage


@dataclass(frozen=True)
class AuthResult:
    """Outcome of an authentication attempt."""

    ok: bool
    error_message: str = ""


def authenticate(
    *,
    auth_mode: str,
    startup: StartupMessage,
    password: str | None = None,
) -> AuthResult:
    """Validate a connection attempt.

    Step 1 implements only ``trust``. Future modes share this signature.
    """

    if auth_mode == "trust":
        return AuthResult(ok=True)

    if auth_mode in {"password", "scram-sha-256"}:
        # Step 6 wiring. Calling these today is a programmer error rather
        # than a client error — fail loudly so the misconfig is obvious.
        raise NotImplementedError(
            f"pgwire auth_mode={auth_mode!r} lands in Step 6 alongside "
            "the unified auth keystore (design/PLAN_unified_auth.md)."
        )

    return AuthResult(
        ok=False,
        error_message=f"Unknown pgwire auth_mode: {auth_mode!r}",
    )
