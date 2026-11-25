"""Auth configuration singleton + the shared ``authenticate()`` validator.

Mirrors the ``api/deps.py`` singleton pattern: ``init_auth()`` is called at
startup (from ``init_session_manager``, so test fixtures get it without the
ASGI lifespan), validates loudly, and stores module state. Every surface
adapter calls ``authenticate(credential)`` and dispatches on the configured
mode. See design/PLAN_authentication.md §1-§3.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import httpx

from orionbelt.auth.bi_tokens import TokenStore, is_bi_token
from orionbelt.auth.errors import (
    AuthConfigError,
    AuthError,
    AuthInvalidError,
    AuthRequiredError,
    TokenInvalidError,
)
from orionbelt.auth.jwt_verify import JwtVerifier, looks_like_jwt
from orionbelt.auth.keys import (
    KeyStore,
    find_low_strength_keys,
    find_weak_keys,
    key_id,
    parse_keys,
)
from orionbelt.auth.oidc import OidcClient, OidcConfig
from orionbelt.auth.principal import ANONYMOUS, API_KEY_PRINCIPAL, Principal
from orionbelt.auth.sealing import Sealer

logger = logging.getLogger("orionbelt.auth")

MODE_NONE = "none"
MODE_API_KEY = "api_key"
MODE_OIDC = "oidc"
_VALID_MODES = frozenset({MODE_NONE, MODE_API_KEY, MODE_OIDC})

DEFAULT_HEADER_NAME = "X-API-Key"
_REVOKE_TIMEOUT_SECONDS = 10.0

# --- module singleton ---
_mode: str = MODE_NONE
_key_store: KeyStore = KeyStore(frozenset())
_header_name: str = DEFAULT_HEADER_NAME
_verifier: JwtVerifier | None = None
_oidc: OidcConfig | None = None
_tokens: TokenStore | None = None


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
    global _mode, _key_store, _header_name, _verifier, _oidc, _tokens  # noqa: PLW0603

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
        logger.info(
            "Auth: api_key; %d key(s) (%s), read from header %s",
            len(keys),
            ", ".join(_key_store.ids()),
            header,
        )
    elif mode == MODE_OIDC:
        if oidc is None:
            raise AuthConfigError("AUTH_MODE=oidc requires OIDC_ISSUER and OIDC_AUDIENCE.")
        # API keys are optional here: service keys next to the IdP's tokens.
        keys = parse_keys(api_keys)
        if keys:
            _check_key_strength(keys)
            dotted = sorted(key_id(k) for k in keys if "." in k)
            if dotted:
                raise AuthConfigError(
                    f"With AUTH_MODE=oidc an API key may not contain '.', since a key "
                    f"shaped like a JWT would be read as one. Found: {', '.join(dotted)}."
                )
        new_verifier = verifier or JwtVerifier(oidc)
        new_verifier.load()
        _key_store = KeyStore(keys)
        logger.info(
            "Auth: oidc; issuer %s; audience %s; %d service key(s)%s",
            oidc.issuer,
            oidc.audience,
            len(keys),
            f" ({', '.join(_key_store.ids())})" if keys else "",
        )
    else:
        _key_store = KeyStore(frozenset())
        logger.info("Auth: disabled (AUTH_MODE=none)")

    _mode = mode
    _header_name = header
    _verifier = new_verifier
    _oidc = oidc if mode == MODE_OIDC else None
    _tokens = (
        TokenStore(
            oidc.token_store_path,
            default_days=oidc.token_default_days,
            max_days=oidc.token_max_days,
            sealer=Sealer(oidc.token_store_keys) if oidc.token_store_keys else None,
            on_sign_in_revoked=_revoke_at_idp,
        )
        if oidc is not None and mode == MODE_OIDC and oidc.token_store_path
        else None
    )
    if _tokens is not None:
        try:
            dropped = _tokens.sweep()
            active, signed_in = _tokens.counts()
        except sqlite3.Error as exc:
            logger.warning("BI token store %s could not be read: %s", _tokens.path, exc)
        else:
            if dropped:
                logger.info("Dropped %d warehouse sign-in(s) of revoked or expired tokens", dropped)
            missing = warehouse_sign_in_missing()
            logger.info(
                "Auth: BI tokens on (store %s: %d active, %d with a warehouse sign-in; "
                "warehouse sign-ins %s)",
                _tokens.path,
                active,
                signed_in,
                f"need {', '.join(missing)}" if missing else "can be made",
            )
    elif mode == MODE_OIDC:
        logger.info("Auth: BI tokens off (TOKEN_STORE_PATH is empty)")


def _check_key_strength(keys: frozenset[str]) -> None:
    """Refuse short or low-entropy API keys (names them by their ids only)."""
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
    global _mode, _key_store, _header_name, _verifier, _oidc, _tokens  # noqa: PLW0603
    _mode = MODE_NONE
    _key_store = KeyStore(frozenset())
    _header_name = DEFAULT_HEADER_NAME
    _verifier = None
    _oidc = None
    _tokens = None


def get_mode() -> str:
    """Return the effective auth mode (``none`` / ``api_key`` / ``oidc``)."""
    return _mode


def get_header_name() -> str:
    """Return the REST header name credentials are read from."""
    return _header_name


def get_oidc_metadata() -> dict[str, Any] | None:
    """The issuer's discovery document under ``oidc``, when it was read; else None."""
    return _verifier.metadata if _verifier is not None else None


def check_still_valid(principal: Principal | None) -> None:
    """Raise when ``principal`` signed in with a BI token that is no longer valid.

    For a connection authenticated once and kept open (the Postgres
    protocol); a caller without a BI token passes. REST and Flight check the
    credential on every call anyway.

    Raises:
        TokenInvalidError: The BI token was revoked, expired, or is gone.
    """
    if principal is None or principal.token_id is None:
        return
    if _tokens is None:
        raise TokenInvalidError("BI tokens are not enabled (TOKEN_STORE_PATH)")
    _tokens.check_active(principal.token_id)


def get_token_store() -> TokenStore | None:
    """The BI token store, under ``oidc`` when ``TOKEN_STORE_PATH`` is set."""
    return _tokens


def get_oidc_client() -> OidcClient | None:
    """OBSL's own client at the identity provider, under ``oidc`` when configured."""
    return _oidc.client if _oidc is not None else None


def get_authorization_endpoint() -> str | None:
    """The identity provider's sign-in endpoint: ``OIDC_AUTHORIZATION_ENDPOINT`` or discovered."""
    return _endpoint(
        _oidc.authorization_endpoint if _oidc is not None else "", "authorization_endpoint"
    )


def get_token_endpoint() -> str | None:
    """The identity provider's token endpoint: ``OIDC_TOKEN_ENDPOINT``, else discovered."""
    return _endpoint(_oidc.token_endpoint if _oidc is not None else "", "token_endpoint")


def _endpoint(explicit: str, discovered: str) -> str | None:
    """An identity provider endpoint: the setting, else the discovery document's entry."""
    if explicit:
        return explicit
    value = (get_oidc_metadata() or {}).get(discovered)
    return value if isinstance(value, str) and value else None


def get_revocation_endpoint() -> str | None:
    """Where the identity provider revokes tokens (RFC 7009): the setting, else discovered."""
    return _endpoint(_oidc.revocation_endpoint if _oidc is not None else "", "revocation_endpoint")


def get_audience() -> str:
    """``OIDC_AUDIENCE``: the audience OBSL's tokens are issued for."""
    return _oidc.audience if _oidc is not None else ""


def get_consent_scopes() -> str:
    """What OBSL's own sign-in for a BI token's warehouse sign-in asks for."""
    return _oidc.consent_scopes if _oidc is not None else ""


def get_consent_redirect_uri() -> str:
    """Where the identity provider sends that sign-in back; empty: derived from the request."""
    return _oidc.consent_redirect_uri if _oidc is not None else ""


def verify_access_token(token: str) -> Principal:
    """The user an identity provider access token is for. Raises ``TokenInvalidError``."""
    if _verifier is None:
        raise TokenInvalidError("AUTH_MODE=oidc is not initialised")
    return _verifier.verify(token)


def warehouse_sign_in_missing() -> list[str]:
    """What a BI token's warehouse sign-in still needs; empty when it can be made.

    It needs the token store with a key to encrypt sign-ins, OBSL's own
    client at the identity provider, and where users sign in and codes are
    exchanged there.
    """
    if _mode != MODE_OIDC:
        return ["AUTH_MODE=oidc"]
    missing = []
    if _tokens is None:
        missing.append("TOKEN_STORE_PATH")
    elif _tokens.sealer is None:
        missing.append("TOKEN_STORE_KEY")
    if get_oidc_client() is None:
        missing.append("OIDC_CLIENT_ID and OIDC_CLIENT_SECRET")
    if get_authorization_endpoint() is None:
        missing.append("OIDC_AUTHORIZATION_ENDPOINT")
    if get_token_endpoint() is None:
        missing.append("OIDC_TOKEN_ENDPOINT")
    return missing


def _revoke_at_idp(refresh_token: str) -> None:
    """Revoke a BI token's dropped warehouse sign-in at the identity provider too.

    Best effort, RFC 7009, as OBSL's own client: an identity provider without
    a revocation endpoint (Entra ID) ends it once it goes unused, and the
    store holds it no longer either way. Never logs the token.
    """
    client = get_oidc_client()
    endpoint = get_revocation_endpoint()
    if client is None or endpoint is None:
        logger.info("A BI token's warehouse sign-in was dropped; no revocation endpoint to call")
        return
    try:
        response = httpx.post(
            endpoint,
            data={"token": refresh_token, "token_type_hint": "refresh_token"},
            auth=(quote(client.client_id, safe=""), quote(client.client_secret, safe="")),
            timeout=_REVOKE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Could not revoke a BI token's sign-in at the IdP: %s", type(exc).__name__)
        return
    if response.status_code >= 400:
        logger.warning(
            "The IdP refused to revoke a BI token's sign-in (HTTP %d)", response.status_code
        )


def get_oidc_config() -> OidcConfig | None:
    """The OIDC settings under ``oidc``, else None."""
    return _oidc


def get_signing_keys_status() -> dict[str, Any] | None:
    """Where the IdP's signing keys come from, how many, when last fetched; None without oidc."""
    return _verifier.keys_status() if _verifier is not None else None


def get_key_last_used() -> dict[str, float | None]:
    """Each API key's id, and when it last authenticated a caller in this process."""
    return _key_store.last_used()


def api_key_caller(credential: str | None) -> Principal | None:
    """The caller an API key authenticates as, named by the key's id, recording its use."""
    identifier = _key_store.match(credential or "")
    return replace(API_KEY_PRINCIPAL, key_id=identifier) if identifier is not None else None


def caller_for_key(key: str) -> Principal:
    """The caller of the key a pgwire SCRAM proof matched, recording its use."""
    identifier = _key_store.id_of(key)
    if identifier is not None:
        _key_store.used(identifier)
    return replace(API_KEY_PRINCIPAL, key_id=identifier)


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
      (``TokenInvalidError`` when it does not verify), a BI token against the
      token store; anything else is checked against the service keys, when
      any are configured.
    """
    if _mode == MODE_NONE:
        return ANONYMOUS

    if _mode == MODE_API_KEY:
        if not credential:
            raise AuthRequiredError("API key required")
        caller = api_key_caller(credential)
        if caller is None:
            raise AuthInvalidError("Invalid API key")
        return caller

    if not credential:
        raise AuthRequiredError("Bearer token required")
    if is_bi_token(credential):
        if _tokens is None:
            raise TokenInvalidError("BI tokens are not enabled (TOKEN_STORE_PATH)")
        return _tokens.authenticate(credential)
    if looks_like_jwt(credential):
        if _verifier is None:  # init_auth always sets it in oidc mode
            raise AuthConfigError("AUTH_MODE=oidc is not initialised")
        return _verifier.verify(credential)
    if len(_key_store):
        caller = api_key_caller(credential)
        if caller is None:
            raise AuthInvalidError("Invalid API key")
        return caller
    raise TokenInvalidError("Token is malformed")
