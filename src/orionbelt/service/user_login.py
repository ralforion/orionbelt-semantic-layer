"""Logging in to a data source as the signed-in user.

A named data source with ``DB_<NAME>_AUTH=user`` is queried as the caller,
not as one shared service account: OBSL exchanges the caller's identity
provider token for a token issued *for the warehouse*, and connects with it.
The warehouse then applies that user's grants and row-level security, which
OBSL cannot know - so connections are not shared between users, and cached
results are kept per user.

Supported here: Databricks (OAuth token federation: an RFC 8693 token
exchange at the workspace's own ``/oidc/v1/token``) and Snowflake (External
OAuth: Entra ID issues the Snowflake token through its on-behalf-of flow,
asked by OBSL's own client there). See
design/PLAN_sso_database_passthrough.md and docs/guide/data-sources.md.

The caller's token is read from the request context
(:func:`orionbelt.auth.current_credential`); it is never logged, cached or
stored. Exchanged tokens are kept in process memory per (data source, user)
until shortly before they expire.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from orionbelt.auth import (
    current_credential,
    current_principal,
    get_oidc_client,
    get_token_endpoint,
)

logger = logging.getLogger("orionbelt.user_login")

USER_LOGIN = "user"
SERVICE_LOGIN = "service"

#: Exchange again this many seconds before a token expires.
_RENEW_MARGIN_SECONDS = 60.0
_EXCHANGE_TIMEOUT_SECONDS = 10.0
#: Default cap on how long a ``user`` source's results stay cached: OBSL is
#: not told when a user's grants change, so a revocation takes effect at most
#: this late (for that user's own cached rows).
DEFAULT_MAX_CACHE_TTL_SECONDS = 300.0

_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: ``DB_<NAME>_OAUTH_EXCHANGE`` for Entra ID's on-behalf-of flow.
ENTRA_OBO = "entra_obo"
#: Identity provider errors that the user resolves by signing in again: MFA
#: or conditional access (Entra adds a claims challenge), or consent.
_INTERACTION_ERRORS = frozenset({"interaction_required", "consent_required"})


class DataSourceSignInError(Exception):
    """Logging in to a data source as the user failed. Carries a code and an HTTP status."""

    code = "DATA_SOURCE_SIGN_IN_FAILED"
    status = 403

    def __init__(self, message: str) -> None:
        super().__init__(f"[{self.code}] {message}")
        self.message = message


class SignInRequiredError(DataSourceSignInError):
    """The data source logs in as the user, and the caller is no signed-in user."""

    code = "SIGN_IN_REQUIRED"
    status = 401


class SignInRefusedError(DataSourceSignInError):
    """The warehouse's token service refused to issue a token for this user."""

    code = "DATA_SOURCE_SIGN_IN_REFUSED"
    status = 403


class SignInInteractionRequiredError(DataSourceSignInError):
    """The identity provider wants the user at its sign-in page first.

    Multi-factor authentication, a conditional access policy or a missing
    consent. ``claims`` is the challenge Entra adds, which the client presents
    when it signs the user in again (a JSON string), or None.
    """

    code = "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED"
    status = 401

    def __init__(self, message: str, claims: str | None = None) -> None:
        super().__init__(message)
        self.claims = claims


class SignInUnavailableError(DataSourceSignInError):
    """The warehouse's token service could not be reached or answered nonsense."""

    code = "DATA_SOURCE_SIGN_IN_UNAVAILABLE"
    status = 503


def logs_in_as_user(source: Any) -> bool:
    """Whether ``source`` (an ``ob_flight`` DataSource) logs in as the caller."""
    return getattr(source, "login", SERVICE_LOGIN) == USER_LOGIN


def _caller() -> tuple[str, str]:
    """The signed-in caller's subject and token. Raises SignInRequiredError."""
    principal = current_principal()
    credential = current_credential()
    if principal is None or principal.sub is None or not credential:
        raise SignInRequiredError(
            "This data source logs in as the signed-in user; call with the identity "
            "provider's access token (AUTH_MODE=oidc), not an API key."
        )
    return principal.sub, credential


def caller_scope() -> str:
    """The user part of a cache scope: a hash of the caller, never the raw ``sub``."""
    sub, _ = _caller()
    return hashlib.sha256(f"sub:{sub}".encode()).hexdigest()[:32]


def max_cache_ttl(source: Any) -> float | None:
    """The TTL cap for ``source``'s cached results; None for a service-login source."""
    if not logs_in_as_user(source):
        return None
    try:
        return float(source.cache_max_ttl_seconds(DEFAULT_MAX_CACHE_TTL_SECONDS))
    except ValueError:
        return DEFAULT_MAX_CACHE_TTL_SECONDS


# --- exchanged tokens --------------------------------------------------------


@dataclass(frozen=True)
class _Issued:
    token: str
    expires_at: float  # monotonic


_tokens: dict[tuple[str, str], _Issued] = {}
_locks: dict[tuple[str, str], threading.Lock] = {}
_tokens_lock = threading.Lock()


def reset_tokens() -> None:
    """Forget every exchanged token (tests, shutdown)."""
    with _tokens_lock:
        _tokens.clear()
        _locks.clear()


def database_token(source: Any) -> str:
    """A token for ``source``'s warehouse, issued for the caller.

    Reused until a minute before it expires, then exchanged again from the
    current request's token. One exchange at a time per (source, user).
    """
    sub, credential = _caller()
    key = (source.fingerprint(), sub)
    with _tokens_lock:
        issued = _tokens.get(key)
        lock = _locks.setdefault(key, threading.Lock())
    if issued is not None and issued.expires_at - _RENEW_MARGIN_SECONDS > time.monotonic():
        return issued.token
    with lock:
        with _tokens_lock:
            issued = _tokens.get(key)
        if issued is not None and issued.expires_at - _RENEW_MARGIN_SECONDS > time.monotonic():
            return issued.token
        token, expires_in = _exchange(source, credential)
        issued = _Issued(token, time.monotonic() + expires_in)
        with _tokens_lock:
            now = time.monotonic()
            for stale in [k for k, v in _tokens.items() if v.expires_at <= now]:
                # The lock goes with the token, so neither grows with every
                # user the process has ever seen.
                del _tokens[stale]
                if stale != key:
                    _locks.pop(stale, None)
            _tokens[key] = issued
        return issued.token


def connect_overrides(source: Any) -> dict[str, Any]:
    """The ``connect()`` arguments that log ``source`` in as the caller."""
    if source.dialect == "databricks":
        return {"access_token": database_token(source)}
    if source.dialect == "snowflake":
        # The token says who connects; a service user or password configured
        # for the source does not go along.
        return {
            "authenticator": "oauth",
            "token": database_token(source),
            "user": None,
            "password": None,
        }
    raise SignInRefusedError(  # configured_datasources refuses this; defence in depth
        f"Data source '{source.name}' ({source.dialect}) cannot log in as the user."
    )


def _exchange(source: Any, subject_token: str) -> tuple[str, float]:
    if source.dialect == "databricks":
        return _exchange_databricks(source, subject_token)
    if source.dialect == "snowflake" and source.oauth_exchange() == ENTRA_OBO:
        return _exchange_entra_obo(source, subject_token)
    raise SignInRefusedError(f"Data source '{source.name}' cannot log in as the user.")


def _exchange_databricks(source: Any, subject_token: str) -> tuple[str, float]:
    """RFC 8693 exchange at the workspace: the user's IdP token for a Databricks token.

    The account's federation policy decides which issuer and audience it
    accepts and which claim names the Databricks user. A service-principal
    policy also needs ``DB_<NAME>_OAUTH_CLIENT_ID``.
    """
    host = (source.getenv("DATABRICKS_SERVER_HOSTNAME") or "").strip()
    if not host:
        raise SignInUnavailableError(
            f"Data source '{source.name}' has no DATABRICKS_SERVER_HOSTNAME to sign in at."
        )
    form = {
        "grant_type": _TOKEN_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": _JWT_TOKEN_TYPE,
        "scope": "all-apis",
    }
    client_id = source.oauth_client_id()
    if client_id:
        form["client_id"] = client_id
    host = host.removeprefix("https://").rstrip("/")
    return _post_token_request(source, f"https://{host}/oidc/v1/token", form)


def _exchange_entra_obo(source: Any, subject_token: str) -> tuple[str, float]:
    """Entra ID's on-behalf-of flow: the user's token for one issued for the warehouse.

    OBSL asks as its own confidential client, the one the user's token was
    issued for (``OIDC_AUDIENCE``), for ``DB_<NAME>_OAUTH_SCOPE``: for
    Snowflake the warehouse's app ID URI and the role scope, such as
    ``api://<Snowflake app ID>/session:role-any``.
    """
    client = get_oidc_client()
    endpoint = get_token_endpoint()
    if client is None or endpoint is None:  # startup refuses this; defence in depth
        raise SignInUnavailableError(
            f"Data source '{source.name}' logs in as the user through the identity "
            "provider, and OBSL has no client there (OIDC_CLIENT_ID) or no token endpoint."
        )
    form = {
        "grant_type": _JWT_BEARER,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "assertion": subject_token,
        "scope": source.oauth_scope() or "",
        "requested_token_use": "on_behalf_of",
    }
    return _post_token_request(source, endpoint, form)


def _post_token_request(source: Any, url: str, form: Mapping[str, str]) -> tuple[str, float]:
    """POST a token request; return (access token, seconds valid). Never logs a token."""
    try:
        response = httpx.post(url, data=dict(form), timeout=_EXCHANGE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise SignInUnavailableError(
            f"Could not reach the sign-in service of data source '{source.name}': "
            f"{type(exc).__name__}."
        ) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        error = body.get("error", f"HTTP {response.status_code}") if isinstance(body, dict) else ""
        description = body.get("error_description", "") if isinstance(body, dict) else ""
        detail = f"{error}: {description}" if description else str(error)
        if response.status_code >= 500:
            raise SignInUnavailableError(
                f"The sign-in service of data source '{source.name}' failed ({detail})."
            )
        if isinstance(body, dict) and body.get("error") in _INTERACTION_ERRORS:
            claims = body.get("claims")
            raise SignInInteractionRequiredError(
                f"Data source '{source.name}' needs the user to sign in again first ({detail}).",
                claims=claims if isinstance(claims, str) and claims else None,
            )
        raise SignInRefusedError(
            f"Data source '{source.name}' refused to log in this user ({detail})."
        )
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise SignInUnavailableError(
            f"The sign-in service of data source '{source.name}' returned no access token."
        )
    expires_in = body.get("expires_in", 3600)
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        seconds = 3600.0
    logger.info("Signed in to data source '%s' as the user (%ds)", source.name, int(seconds))
    return token, seconds
