"""How a Postgres wire client logs in: the password mechanism, and its description.

Kept with authentication, below both the Postgres wire surface (which offers
the mechanism) and the REST API (whose ``GET /v1/auth/status`` describes it),
so the two can never disagree.
"""

from __future__ import annotations

from orionbelt.auth.config import MODE_NONE, MODE_OIDC, get_mode

# Password mechanisms offered when a credential is required.
MECH_SCRAM = "scram-sha-256"
MECH_CLEARTEXT = "cleartext"


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


def login_summary(pgwire_auth_mode: str, *, tls: bool) -> str:
    """How a client logs in, for the startup line and ``GET /v1/auth/status``."""
    if get_mode() == MODE_NONE:
        return "AUTH_MODE=none, no password"
    mechanism = select_mechanism(pgwire_auth_mode, tls=tls)
    how = "cleartext password" if mechanism == MECH_CLEARTEXT else "SCRAM-SHA-256"
    return f"AUTH_MODE={get_mode()}, {how}"
