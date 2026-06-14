"""Auth error codes and exception types.

Error-code string constants follow the repo convention of inline codes
raised at their use site (see design/PLAN_authentication.md §7). The
exception types let adapters translate a single ``authenticate()`` failure
into a protocol-appropriate response (HTTP 401/403, gRPC Unauthenticated,
pgwire ErrorResponse) without each adapter re-deriving the reason.
"""

from __future__ import annotations

# Stable error codes (mirrors the table in PLAN_authentication.md §7).
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_INVALID = "AUTH_INVALID"
AUTH_CONFIG_ERROR = "AUTH_CONFIG_ERROR"
AUTH_INSUFFICIENT_SCOPE = "AUTH_INSUFFICIENT_SCOPE"


class AuthError(Exception):
    """Base class for all auth failures. Carries a stable ``code``."""

    code: str = AUTH_INVALID

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AuthConfigError(AuthError):
    """Misconfiguration detected at startup (fail loudly, refuse to start)."""

    code = AUTH_CONFIG_ERROR


class AuthRequiredError(AuthError):
    """A credential is required (non-``none`` mode) but none was provided."""

    code = AUTH_REQUIRED


class AuthInvalidError(AuthError):
    """A credential was provided but did not validate."""

    code = AUTH_INVALID


class InsufficientScopeError(AuthError):
    """The principal authenticated but lacks a required scope (oidc)."""

    code = AUTH_INSUFFICIENT_SCOPE

    def __init__(self, required_scope: str) -> None:
        super().__init__(f"Insufficient scope: requires {required_scope}")
        self.required_scope = required_scope
