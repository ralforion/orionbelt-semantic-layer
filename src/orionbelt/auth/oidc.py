"""OIDC configuration and the mapping from token claims to OBSL scopes.

Pure functions and a frozen config, no I/O: ``jwt_verify.py`` does the
fetching and verifying. See docs/guide/authentication.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orionbelt.auth.errors import AuthConfigError
from orionbelt.auth.principal import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE

# The scopes OBSL knows, each with the ones it implies.
_IMPLIED: dict[str, frozenset[str]] = {
    SCOPE_READ: frozenset({SCOPE_READ}),
    SCOPE_WRITE: frozenset({SCOPE_WRITE, SCOPE_READ}),
    SCOPE_ADMIN: frozenset({SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ}),
}

DEFAULT_SCOPE_CLAIMS = ("scope", "scp", "roles")


@dataclass(frozen=True)
class OidcClient:
    """OBSL's own confidential client at the identity provider.

    Asks the identity provider for a warehouse token on a signed-in user's
    behalf, where the identity provider issues it (Snowflake: Entra's
    on-behalf-of flow). The secret is left out of ``repr``, so it reaches no
    log line through this object.
    """

    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class OidcConfig:
    """What ``AUTH_MODE=oidc`` verifies tokens against."""

    issuer: str
    audience: str
    jwks_uri: str = ""
    jwks_file: str = ""
    jwks_cache_ttl: int = 86400
    jwks_min_refetch_seconds: int = 60
    scope_claims: tuple[str, ...] = DEFAULT_SCOPE_CLAIMS
    # IdP value (a scope or role name) -> the OBSL scopes it grants.
    scope_map: Mapping[str, frozenset[str]] = field(default_factory=dict)
    clock_skew_seconds: int = 60
    # BI tokens (TOKEN_STORE_PATH, empty: none), with their default and
    # longest lifetime in days.
    token_store_path: str = ""
    token_default_days: int = 90
    token_max_days: int = 365
    # The sign-in and token endpoints when set (OIDC_AUTHORIZATION_ENDPOINT,
    # OIDC_TOKEN_ENDPOINT), else the discovery document's; and OBSL's own
    # client there, when one is configured.
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    client: OidcClient | None = None

    def __post_init__(self) -> None:
        if not self.issuer.strip():
            raise AuthConfigError("AUTH_MODE=oidc requires OIDC_ISSUER.")
        if not self.audience.strip():
            raise AuthConfigError(
                "AUTH_MODE=oidc requires OIDC_AUDIENCE (the API's audience at the IdP)."
            )
        if not self.scope_claims:
            raise AuthConfigError("OIDC_SCOPE_CLAIMS names no claim.")
        if not 1 <= self.token_default_days <= self.token_max_days:
            raise AuthConfigError(
                "TOKEN_DEFAULT_DAYS must be at least 1 and at most TOKEN_MAX_DAYS."
            )

    @property
    def discovery_url(self) -> str:
        """The issuer's OpenID Provider metadata document."""
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


def parse_scope_claims(raw: str) -> tuple[str, ...]:
    """Split ``OIDC_SCOPE_CLAIMS`` (comma-separated claim paths)."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_scope_map(raw: str) -> dict[str, frozenset[str]]:
    """Parse ``OIDC_SCOPE_MAP``: comma-separated ``<IdP value>=<OBSL scope>`` pairs.

    A name may appear more than once to grant several scopes. Raises
    :class:`AuthConfigError` on a malformed pair or an unknown OBSL scope.
    """
    result: dict[str, set[str]] = {}
    for pair in (p.strip() for p in raw.split(",")):
        if not pair:
            continue
        name, sep, scope = (part.strip() for part in pair.partition("="))
        if not sep or not name or not scope:
            raise AuthConfigError(
                f"OIDC_SCOPE_MAP entry {pair!r} is not of the form <IdP value>=<OBSL scope>."
            )
        if scope not in _IMPLIED:
            raise AuthConfigError(
                f"OIDC_SCOPE_MAP maps {name!r} to {scope!r}, which is not an OBSL scope "
                f"(expected one of: {', '.join(sorted(_IMPLIED))})."
            )
        result.setdefault(name, set()).add(scope)
    return {name: frozenset(scopes) for name, scopes in result.items()}


def _claim_at(claims: Mapping[str, Any], path: str) -> Any:
    """Return the claim at a dot path (``realm_access.roles``), or None."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _values(raw: Any) -> Iterable[str]:
    """A claim's values: a space-separated string (``scope``) or a list of strings."""
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return (item for item in raw if isinstance(item, str))
    return ()


def scopes_from_claims(claims: Mapping[str, Any], config: OidcConfig) -> frozenset[str]:
    """Return the OBSL scopes a verified token grants.

    Collects the values of every claim in ``config.scope_claims``. A value
    that is an OBSL scope counts as itself; any value in ``config.scope_map``
    grants what it maps to. ``obsl:admin`` implies ``obsl:write``, which
    implies ``obsl:read``.
    """
    granted: set[str] = set()
    for path in config.scope_claims:
        for value in _values(_claim_at(claims, path)):
            if value in _IMPLIED:
                granted.add(value)
            granted.update(config.scope_map.get(value, ()))
    return with_implied(granted)


def with_implied(scopes: Iterable[str]) -> frozenset[str]:
    """``scopes`` with those they imply: admin implies write, write implies read."""
    return frozenset().union(*(_IMPLIED[scope] for scope in scopes if scope in _IMPLIED))
