"""Shared authentication subsystem.

One validator behind every surface (REST, Flight, pgwire) and consumer
(UI, MCP). Each surface adapter parses its protocol-native credential and
calls :func:`authenticate`; the configured ``AUTH_MODE`` decides how the
credential is checked. See design/PLAN_authentication.md.
"""

from __future__ import annotations

from orionbelt.auth.config import (
    DEFAULT_HEADER_NAME,
    MODE_API_KEY,
    MODE_NONE,
    MODE_OIDC,
    authenticate,
    get_api_keys,
    get_header_name,
    get_mode,
    init_auth,
    reset_auth,
    resolve_mode,
    validate_credential,
)
from orionbelt.auth.errors import (
    AUTH_CONFIG_ERROR,
    AUTH_INSUFFICIENT_SCOPE,
    AUTH_INVALID,
    AUTH_REQUIRED,
    AuthConfigError,
    AuthError,
    AuthInvalidError,
    AuthRequiredError,
    InsufficientScopeError,
)
from orionbelt.auth.principal import (
    FULL_SCOPES,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    Principal,
)

__all__ = [
    "AUTH_CONFIG_ERROR",
    "AUTH_INSUFFICIENT_SCOPE",
    "AUTH_INVALID",
    "AUTH_REQUIRED",
    "DEFAULT_HEADER_NAME",
    "FULL_SCOPES",
    "MODE_API_KEY",
    "MODE_NONE",
    "MODE_OIDC",
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "AuthConfigError",
    "AuthError",
    "AuthInvalidError",
    "AuthRequiredError",
    "InsufficientScopeError",
    "Principal",
    "authenticate",
    "get_api_keys",
    "get_header_name",
    "get_mode",
    "init_auth",
    "reset_auth",
    "resolve_mode",
    "validate_credential",
]
