"""Verify OIDC access tokens (JWT) against the issuer's published keys.

The key set comes from the issuer's discovery document, from
``OIDC_JWKS_URI``, or from ``OIDC_JWKS_FILE`` (air-gapped: never fetched).
It is loaded once at startup, which fails loudly when it cannot be, and
fetched again when it is older than ``OIDC_JWKS_CACHE_TTL`` or when a token
names a key id it does not hold (key rotation), at most once per
``OIDC_JWKS_MIN_REFETCH_SECONDS`` so random key ids cannot force a fetch per
request. A failed refetch keeps the keys already held.

Verification is synchronous and may fetch; REST calls it from a worker
thread (``api/auth.py``), never on the event loop.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

from orionbelt.auth.errors import AuthConfigError, TokenInvalidError
from orionbelt.auth.oidc import OidcConfig, scopes_from_claims
from orionbelt.auth.principal import Principal

logger = logging.getLogger("orionbelt.auth")

# Asymmetric only: a symmetric (HS*) key would have to be a shared secret, and
# "none" is no signature at all.
ALLOWED_ALGORITHMS = ("RS256", "PS256", "ES256", "EdDSA")
_FETCH_TIMEOUT_SECONDS = 5.0
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def looks_like_jwt(credential: str) -> bool:
    """True when ``credential`` has a JWT's shape (three base64url parts)."""
    return bool(_JWT_SHAPE.match(credential))


def fetch_json(url: str) -> dict[str, Any]:
    """GET ``url`` and return its JSON object. Raises on any failure."""
    response = httpx.get(url, timeout=_FETCH_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return data


@dataclass(frozen=True)
class _Key:
    jwk: jwt.PyJWK
    declared_alg: str | None  # the JWK's own "alg", when it has one


def _parse_key_set(data: Mapping[str, Any]) -> dict[str | None, _Key]:
    """Index a JWK Set's signing keys by key id; skip keys PyJWT cannot use."""
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, list):
        raise ValueError('the key set has no "keys" list')
    keys: dict[str | None, _Key] = {}
    for raw in raw_keys:
        if not isinstance(raw, dict) or raw.get("use") == "enc":
            continue
        try:
            jwk = jwt.PyJWK(raw)
        except jwt.PyJWKError:
            continue
        alg = raw.get("alg")
        keys[jwk.key_id] = _Key(jwk, alg if isinstance(alg, str) else None)
    if not keys:
        raise ValueError("the key set holds no usable signing key")
    return keys


class _KeySet:
    """The issuer's signing keys, refreshed as described in the module docstring."""

    def __init__(self, config: OidcConfig, fetch: Callable[[str], dict[str, Any]]) -> None:
        self._config = config
        self._fetch = fetch
        self._source: str | None = None  # None: keys come from OIDC_JWKS_FILE
        # The issuer's discovery document, when the keys were found through it.
        self.metadata: dict[str, Any] | None = None
        self._keys: dict[str | None, _Key] = {}
        self._fetched_at = 0.0
        self._loaded_wall: float | None = None  # unix time the keys were last (re)loaded
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    def load(self) -> None:
        """Resolve where the keys come from and load them. Raises AuthConfigError."""
        config = self._config
        try:
            if config.jwks_file:
                self._keys = _parse_key_set(json.loads(Path(config.jwks_file).read_text()))
                self._loaded_wall = time.time()
                logger.info(
                    "Auth: oidc keys loaded from OIDC_JWKS_FILE (%d key(s))", len(self._keys)
                )
                return
            source = config.jwks_uri or self._discover_jwks_uri()
            self._keys = _parse_key_set(self._fetch(source))
        except AuthConfigError:
            raise
        except Exception as exc:  # any fetch / parse failure: refuse to start
            raise AuthConfigError(f"AUTH_MODE=oidc could not load the signing keys: {exc}") from exc
        self._source = source
        self._fetched_at = self._last_attempt = time.monotonic()
        self._loaded_wall = time.time()
        if not source.startswith("https://"):
            logger.warning("Auth: oidc keys are fetched over plain HTTP from %s", source)
        logger.info("Auth: oidc keys loaded from %s (%d key(s))", source, len(self._keys))

    def _discover_jwks_uri(self) -> str:
        metadata = self._fetch(self._config.discovery_url)
        self.metadata = metadata
        if metadata.get("issuer") != self._config.issuer:
            raise AuthConfigError(
                f"The discovery document at {self._config.discovery_url} names issuer "
                f"{metadata.get('issuer')!r}, not OIDC_ISSUER {self._config.issuer!r}. "
                "They must match exactly (including any trailing slash)."
            )
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise AuthConfigError(f"{self._config.discovery_url} has no jwks_uri")
        return jwks_uri

    def status(self) -> dict[str, Any]:
        """Where the keys come from, how many there are, and when they were last loaded."""
        return {
            "source": self._source or "OIDC_JWKS_FILE",
            "count": len(self._keys),
            "loaded_at": self._loaded_wall,
        }

    def key_for(self, kid: str | None) -> _Key:
        """Return the key a token's ``kid`` names. Raises TokenInvalidError."""
        now = time.monotonic()
        if self._source and now - self._fetched_at > self._config.jwks_cache_ttl:
            self._refresh()
        key = self._lookup(kid)
        if key is None and self._source:
            self._refresh()
            key = self._lookup(kid)
        if key is None:
            raise TokenInvalidError("Token is signed with a key the issuer does not publish")
        return key

    def _lookup(self, kid: str | None) -> _Key | None:
        keys = self._keys
        if kid is None and len(keys) == 1:
            return next(iter(keys.values()))
        return keys.get(kid)

    def _refresh(self) -> None:
        """Fetch the keys again unless that was tried too recently."""
        source = self._source
        if source is None:
            return
        with self._lock:
            now = time.monotonic()
            if now - self._last_attempt < self._config.jwks_min_refetch_seconds:
                return
            self._last_attempt = now
            try:
                self._keys = _parse_key_set(self._fetch(source))
                self._fetched_at = now
                self._loaded_wall = time.time()
            except Exception as exc:
                logger.warning(
                    "Auth: refetching the oidc keys failed, keeping the old ones: %s", exc
                )


class JwtVerifier:
    """Turns a verified access token into a :class:`Principal`."""

    def __init__(
        self,
        config: OidcConfig,
        fetch: Callable[[str], dict[str, Any]] = fetch_json,
    ) -> None:
        self._config = config
        self._keys = _KeySet(config, fetch)

    def load(self) -> None:
        """Load the signing keys (startup). Raises AuthConfigError."""
        self._keys.load()

    @property
    def metadata(self) -> dict[str, Any] | None:
        """The issuer's discovery document, or None when it was not read."""
        return self._keys.metadata

    def keys_status(self) -> dict[str, Any]:
        """Where the signing keys come from, how many, and when they were last loaded."""
        return self._keys.status()

    def verify(self, token: str) -> Principal:
        """Verify ``token`` and return its user principal. Raises TokenInvalidError."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenInvalidError("Token is malformed") from None
        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            accepted = ", ".join(ALLOWED_ALGORITHMS)
            raise TokenInvalidError(
                f"Token algorithm is not accepted (expected one of: {accepted})"
            )
        kid = header.get("kid")
        key = self._keys.key_for(kid if isinstance(kid, str) else None)
        if key.declared_alg is not None and key.declared_alg != alg:
            raise TokenInvalidError("Token algorithm does not match its signing key")
        claims = self._decode(token, key, alg)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise TokenInvalidError("Token has no subject (sub)")
        email = claims.get("email")
        return Principal(
            kind="user",
            sub=sub,
            email=email if isinstance(email, str) else None,
            scopes=scopes_from_claims(claims, self._config),
        )

    def _decode(self, token: str, key: _Key, alg: str) -> dict[str, Any]:
        config = self._config
        try:
            return jwt.decode(
                token,
                key=key.jwk.key,
                algorithms=[alg],
                audience=config.audience,
                issuer=config.issuer,
                leeway=config.clock_skew_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenInvalidError("Token has expired") from None
        except jwt.ImmatureSignatureError:
            raise TokenInvalidError("Token is not valid yet") from None
        except jwt.InvalidIssuerError:
            raise TokenInvalidError("Token issuer does not match OIDC_ISSUER") from None
        except jwt.InvalidAudienceError:
            raise TokenInvalidError("Token audience does not include OIDC_AUDIENCE") from None
        except jwt.MissingRequiredClaimError as exc:
            raise TokenInvalidError(f"Token has no {exc.claim} claim") from None
        except jwt.InvalidSignatureError:
            raise TokenInvalidError("Token signature does not verify") from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise TokenInvalidError("Token is invalid") from None
