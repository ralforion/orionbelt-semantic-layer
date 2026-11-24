"""Authenticated principal returned by every auth validator.

A single ``Principal`` shape flows out of all auth modes so route code
never branches on ``AUTH_MODE``. In ``none`` and ``api_key`` modes the
principal carries the full scope set; in ``oidc`` mode (Phase 4) scopes
come from the verified token. See design/PLAN_authentication.md §2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Scope vocabulary. Every /v1 endpoint requires one (api/route_scopes.py).
# Anonymous and api_key principals carry all three, so the check only
# restricts OIDC users (Phase 4b).
SCOPE_READ = "obsl:read"
SCOPE_WRITE = "obsl:write"
SCOPE_ADMIN = "obsl:admin"

FULL_SCOPES: frozenset[str] = frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN})


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request.

    ``kind`` is one of ``"anonymous"`` (auth off), ``"api_key"`` (a valid
    static key), or ``"user"`` (an OIDC-verified identity, Phase 4).
    ``token_id`` is set when the user called with a BI token rather than an
    identity provider's token (``auth/bi_tokens.py``).
    """

    kind: str
    sub: str | None = None
    email: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    token_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        """Return True when this principal is granted ``scope``."""
        return scope in self.scopes


# Shared singletons for the non-identity modes.
ANONYMOUS = Principal(kind="anonymous", scopes=FULL_SCOPES)
API_KEY_PRINCIPAL = Principal(kind="api_key", scopes=FULL_SCOPES)
