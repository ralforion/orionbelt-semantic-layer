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
    check_still_valid,
    get_api_keys,
    get_authorization_endpoint,
    get_header_name,
    get_mode,
    get_oidc_client,
    get_oidc_metadata,
    get_token_endpoint,
    get_token_store,
    init_auth,
    reset_auth,
    resolve_mode,
    validate_api_key,
    validate_credential,
)
from orionbelt.auth.context import (
    Binding,
    bind_principal,
    current_credential,
    current_principal,
    may_use,
    reset_principal,
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
    TokenInvalidError,
)
from orionbelt.auth.oidc import OidcClient, OidcConfig, parse_scope_claims, parse_scope_map
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
    "OidcClient",
    "OidcConfig",
    "Principal",
    "TokenInvalidError",
    "authenticate",
    "check_still_valid",
    "Binding",
    "bind_principal",
    "current_credential",
    "current_principal",
    "get_api_keys",
    "get_authorization_endpoint",
    "get_header_name",
    "get_mode",
    "get_oidc_client",
    "get_oidc_metadata",
    "get_token_endpoint",
    "get_token_store",
    "init_auth",
    "may_use",
    "parse_scope_claims",
    "parse_scope_map",
    "reset_auth",
    "reset_principal",
    "resolve_mode",
    "validate_api_key",
    "validate_credential",
]
