"""OIDC JWT verification — Phase 4 stub.

This module is intentionally unimplemented in Phase 1. The auth config
rejects ``AUTH_MODE=oidc`` at startup with a clear ``AuthConfigError``
pointing here, so this stub is never reached at request time. Phase 4 fills
in JWKS fetch/cache (``cachetools.TTLCache``) + ``pyjwt[crypto]`` signature
verification (``iss`` / ``aud`` / ``exp`` / ``nbf``, algorithm whitelist
``["RS256", "ES256", "EdDSA"]``). See design/PLAN_authentication.md §6 Phase 4.
"""

from __future__ import annotations

from orionbelt.auth.errors import AuthConfigError
from orionbelt.auth.principal import Principal


def verify_jwt(token: str) -> Principal:  # pragma: no cover - Phase 4
    """Verify an OIDC bearer token and return a user Principal.

    Not implemented in Phase 1.
    """
    raise AuthConfigError(
        "AUTH_MODE=oidc is not implemented yet (Phase 4). Use AUTH_MODE=api_key or AUTH_MODE=none."
    )
