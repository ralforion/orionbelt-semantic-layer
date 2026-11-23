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
    TokenInvalidError,
)
from orionbelt.auth.jwt_verify import JwtVerifier, looks_like_jwt
from orionbelt.auth.keys import KeyStore, find_low_strength_keys, find_weak_keys, parse_keys
from orionbelt.auth.oidc import OidcConfig
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
_verifier: JwtVerifier | None = None


def resolve_mode(auth_mode: str) -> str:
    """Normalise ``AUTH_MODE`` (case, whitespace; empty means ``none``)."""
    return (auth_mode or MODE_NONE).strip().lower()


def init_auth(
    *,
    auth_mode: str = MODE_NONE,
    api_keys: str = "",
    header_name: str = DEFAULT_HEADER_NAME,
    oidc: OidcConfig | None = None,
    verifier: JwtVerifier | None = None,
) -> None:
    """Configure auth state at startup. Raises ``AuthConfigError`` on bad config.

    ``oidc`` is required for ``AUTH_MODE=oidc``; ``verifier`` replaces the
    default :class:`JwtVerifier` built from it (tests inject a fetch).
    """
    global _mode, _key_store, _header_name, _verifier  # noqa: PLW0603

    mode = resolve_mode(auth_mode)
    if mode not in _VALID_MODES:
        raise AuthConfigError(
            f"Invalid AUTH_MODE '{mode}'. Expected one of: {', '.join(sorted(_VALID_MODES))}."
        )

    header = (header_name or DEFAULT_HEADER_NAME).strip() or DEFAULT_HEADER_NAME

    new_verifier: JwtVerifier | None = None
    if mode == MODE_API_KEY:
        keys = parse_keys(api_keys)
        if not keys:
            raise AuthConfigError(
                "AUTH_MODE=api_key but API_KEYS is empty. Set API_KEYS to a "
                "comma-separated list of strong keys (>=32 chars, high-entropy)."
            )
        _check_key_strength(keys)
        _key_store = KeyStore(keys)
        logger.info("Auth: api_key mode; %d key(s) loaded", len(keys))
    elif mode == MODE_OIDC:
        if oidc is None:
            raise AuthConfigError("AUTH_MODE=oidc requires OIDC_ISSUER and OIDC_AUDIENCE.")
        # API keys are optional here: service keys next to the IdP's tokens.
        keys = parse_keys(api_keys)
        if keys:
            _check_key_strength(keys)
            dotted = [f"{k[:4]}..." for k in sorted(keys) if "." in k]
            if dotted:
                raise AuthConfigError(
                    f"With AUTH_MODE=oidc an API key may not contain '.', since a key "
                    f"shaped like a JWT would be read as one. Found: {', '.join(dotted)}."
                )
        new_verifier = verifier or JwtVerifier(oidc)
        new_verifier.load()
        _key_store = KeyStore(keys)
        logger.info("Auth: oidc mode; issuer %s; %d service key(s) loaded", oidc.issuer, len(keys))
    else:
        _key_store = KeyStore(frozenset())
        logger.info("Auth: disabled (AUTH_MODE=none)")

    _mode = mode
    _header_name = header
    _verifier = new_verifier


def _check_key_strength(keys: frozenset[str]) -> None:
    """Refuse short or low-entropy API keys (names them by a 4-char prefix only)."""
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


def reset_auth() -> None:
    """Reset auth state to defaults (for tests / shutdown)."""
    global _mode, _key_store, _header_name, _verifier  # noqa: PLW0603
    _mode = MODE_NONE
    _key_store = KeyStore(frozenset())
    _header_name = DEFAULT_HEADER_NAME
    _verifier = None


def get_mode() -> str:
    """Return the effective auth mode (``none`` / ``api_key`` / ``oidc``)."""
    return _mode


def get_header_name() -> str:
    """Return the REST header name credentials are read from."""
    return _header_name


def get_api_keys() -> frozenset[str]:
    """Return the configured API keys.

    Set in ``api_key`` mode, and in ``oidc`` mode when service keys are
    configured. Exposed for the pgwire SCRAM exchange,
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


def validate_api_key(credential: str | None) -> bool:
    """Return True when ``credential`` is one of the configured API keys.

    For Flight and pgwire, which accept API keys only (also in ``oidc`` mode,
    until they accept the IdP's tokens). False in ``none`` mode, where callers
    do not ask.
    """
    return _key_store.validate(credential or "")


def authenticate(credential: str | None) -> Principal:
    """Validate a bare credential and return the resulting Principal.

    - ``none``: always returns the anonymous principal (credential ignored).
    - ``api_key``: missing credential → ``AuthRequiredError``; wrong key →
      ``AuthInvalidError``; valid key → the api_key principal.
    - ``oidc``: a JWT-shaped credential is verified as the IdP's access token
      (``TokenInvalidError`` when it does not verify); anything else is
      checked against the service keys, when any are configured.
    """
    if _mode == MODE_NONE:
        return ANONYMOUS

    if _mode == MODE_API_KEY:
        if not credential:
            raise AuthRequiredError("API key required")
        if not _key_store.validate(credential):
            raise AuthInvalidError("Invalid API key")
        return API_KEY_PRINCIPAL

    if not credential:
        raise AuthRequiredError("Bearer token required")
    if looks_like_jwt(credential):
        if _verifier is None:  # init_auth always sets it in oidc mode
            raise AuthConfigError("AUTH_MODE=oidc is not initialised")
        return _verifier.verify(credential)
    if len(_key_store):
        if _key_store.validate(credential):
            return API_KEY_PRINCIPAL
        raise AuthInvalidError("Invalid API key")
    raise TokenInvalidError("Token is malformed")
