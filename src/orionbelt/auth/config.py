"""Auth configuration singleton + the shared ``authenticate()`` validator.

Mirrors the ``api/deps.py`` singleton pattern: ``init_auth()`` is called at
startup (from ``init_session_manager``, so test fixtures get it without the
ASGI lifespan), validates loudly, and stores module state. Every surface
adapter calls ``authenticate(credential)`` and dispatches on the configured
mode. See design/PLAN_authentication.md §1-§3.
"""

from __future__ import annotations

import logging

from orionbelt.auth.errors import (
    AuthConfigError,
    AuthError,
    AuthInvalidError,
    AuthRequiredError,
)
from orionbelt.auth.keys import KeyStore, find_low_strength_keys, find_weak_keys, parse_keys
from orionbelt.auth.principal import ANONYMOUS, API_KEY_PRINCIPAL, Principal

logger = logging.getLogger("orionbelt.auth")

MODE_NONE = "none"
MODE_API_KEY = "api_key"
MODE_OIDC = "oidc"
_VALID_MODES = frozenset({MODE_NONE, MODE_API_KEY, MODE_OIDC})

DEFAULT_HEADER_NAME = "X-API-Key"

# --- module singleton ---
_mode: str = MODE_NONE
_key_store: KeyStore = KeyStore(frozenset())
_header_name: str = DEFAULT_HEADER_NAME


def resolve_mode(auth_mode: str, auth_enabled: bool) -> str:
    """Resolve the effective mode from ``AUTH_MODE`` + the legacy alias.

    ``AUTH_ENABLED=true`` is a deprecated alias for ``AUTH_MODE=api_key``.
    It only takes effect when ``AUTH_MODE`` is left at its ``none`` default,
    so an explicit ``AUTH_MODE`` always wins.
    """
    mode = (auth_mode or MODE_NONE).strip().lower()
    if auth_enabled and mode == MODE_NONE:
        logger.warning(
            "AUTH_ENABLED=true is deprecated; use AUTH_MODE=api_key. "
            "Honouring it as api_key for this release."
        )
        return MODE_API_KEY
    return mode


def init_auth(
    *,
    auth_mode: str = MODE_NONE,
    api_keys: str = "",
    header_name: str = DEFAULT_HEADER_NAME,
    auth_enabled: bool = False,
) -> None:
    """Configure auth state at startup. Raises ``AuthConfigError`` on bad config."""
    global _mode, _key_store, _header_name  # noqa: PLW0603

    mode = resolve_mode(auth_mode, auth_enabled)
    if mode not in _VALID_MODES:
        raise AuthConfigError(
            f"Invalid AUTH_MODE '{mode}'. Expected one of: {', '.join(sorted(_VALID_MODES))}."
        )

    header = (header_name or DEFAULT_HEADER_NAME).strip() or DEFAULT_HEADER_NAME

    if mode == MODE_API_KEY:
        keys = parse_keys(api_keys)
        if not keys:
            raise AuthConfigError(
                "AUTH_MODE=api_key but API_KEYS is empty. Set API_KEYS to a "
                "comma-separated list of strong keys (>=32 chars, high-entropy)."
            )
        weak = find_weak_keys(keys)
        if weak:
            raise AuthConfigError(
                f"API keys must be at least 16 characters. Found {len(weak)} weak key(s): "
                f"{', '.join(weak)}."
            )
        weak_strength = find_low_strength_keys(keys)
        if weak_strength:
            raise AuthConfigError(
                f"{len(weak_strength)} API key(s) are too weak ({', '.join(weak_strength)}): "
                "keys must be at least 32 characters and high-entropy (short / low-entropy keys "
                "are vulnerable to offline attack on captured SCRAM transcripts). Generate one "
                "with: python3 -c \"import secrets; print(f'obsl_pat_{secrets.token_hex(20)}')\"."
            )
        _key_store = KeyStore(keys)
        logger.info("Auth: api_key mode; %d key(s) loaded", len(keys))
    elif mode == MODE_OIDC:
        # Phase 1 has no OIDC verifier — fail loudly rather than silently
        # accepting nothing. Phase 4 wires jwt_verify.py + OIDC_* settings.
        raise AuthConfigError(
            "AUTH_MODE=oidc is not implemented yet (Phase 4). "
            "Use AUTH_MODE=api_key or AUTH_MODE=none."
        )
    else:
        _key_store = KeyStore(frozenset())
        logger.info("Auth: disabled (AUTH_MODE=none)")

    _mode = mode
    _header_name = header


def reset_auth() -> None:
    """Reset auth state to defaults (for tests / shutdown)."""
    global _mode, _key_store, _header_name  # noqa: PLW0603
    _mode = MODE_NONE
    _key_store = KeyStore(frozenset())
    _header_name = DEFAULT_HEADER_NAME


def get_mode() -> str:
    """Return the effective auth mode (``none`` / ``api_key`` / ``oidc``)."""
    return _mode


def get_header_name() -> str:
    """Return the REST header name credentials are read from."""
    return _header_name


def get_api_keys() -> frozenset[str]:
    """Return the configured API keys.

    Only meaningful in ``api_key`` mode. Exposed for the pgwire SCRAM exchange,
    which must know the cleartext keys to verify a client proof (the server
    process already holds them — same trust boundary).
    """
    return _key_store.keys


def validate_credential(credential: str | None) -> bool:
    """Return True when ``credential`` authenticates under the current mode.

    A boolean convenience for non-HTTP adapters (Flight, pgwire) that need a
    yes/no rather than a Principal-or-raise. In ``none`` mode every credential
    is accepted (callers gate on mode before relying on this).
    """
    try:
        authenticate(credential)
        return True
    except AuthError:
        return False


def authenticate(credential: str | None) -> Principal:
    """Validate a bare credential and return the resulting Principal.

    - ``none``: always returns the anonymous principal (credential ignored).
    - ``api_key``: missing credential → ``AuthRequiredError``; wrong key →
      ``AuthInvalidError``; valid key → the api_key principal.
    - ``oidc``: Phase 4 (never reached — init refuses to start in this mode).
    """
    if _mode == MODE_NONE:
        return ANONYMOUS

    if _mode == MODE_API_KEY:
        if not credential:
            raise AuthRequiredError("API key required")
        if not _key_store.validate(credential):
            raise AuthInvalidError("Invalid API key")
        return API_KEY_PRINCIPAL

    # _mode == MODE_OIDC — unreachable in Phase 1 (init_auth refuses it).
    from orionbelt.auth.jwt_verify import verify_jwt

    if not credential:
        raise AuthRequiredError("Bearer token required")
    return verify_jwt(credential)
