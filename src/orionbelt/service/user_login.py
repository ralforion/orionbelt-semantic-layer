"""Logging in to a data source as the signed-in user.

A named data source with ``DB_<NAME>_AUTH=user`` is queried as the caller,
not as one shared service account: OBSL exchanges the caller's identity
provider token for a token issued *for the warehouse*, and connects with it.
The warehouse then applies that user's grants and row-level security, which
OBSL cannot know - so connections are not shared between users, and cached
results are kept per user.

Supported here: Databricks (OAuth token federation: an RFC 8693 token
exchange at the workspace's own ``/oidc/v1/token``) and Snowflake (External
OAuth: the identity provider issues the Snowflake token, asked by OBSL's own
client there, through Entra ID's on-behalf-of flow or the standard token
exchange of Okta, Keycloak and others) and BigQuery (workforce identity
federation: Google's security token service exchanges the user's token for a
Google access token). See
design/PLAN_sso_database_passthrough.md and docs/guide/data-sources.md.

The caller's token is read from the request context
(:func:`orionbelt.auth.current_credential`); it is never logged, cached or
stored. Exchanged tokens are kept in process memory per (data source, user)
until shortly before they expire.

A BI tool's caller holds a BI token, which is no identity provider token. A
BI token created with a warehouse sign-in holds the identity provider's
refresh token for its owner (``auth/consent.py``); OBSL redeems it, as its
own client, for the owner's token and exchanges that as above. The minted
token is kept in memory per BI token until shortly before it expires; a
refresh token the identity provider rotates is stored before it is used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from orionbelt.auth import (
    OidcClient,
    current_credential,
    current_principal,
    get_consent_scopes,
    get_oidc_client,
    get_token_endpoint,
    get_token_store,
    warehouse_sign_in_missing,
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
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
#: Google's security token service, and the scope its tokens carry for BigQuery.
_GOOGLE_STS = "https://sts.googleapis.com/v1/token"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: ``DB_<NAME>_OAUTH_EXCHANGE`` for Entra ID's on-behalf-of flow.
ENTRA_OBO = "entra_obo"
#: ``DB_<NAME>_OAUTH_EXCHANGE`` for the standard token exchange (RFC 8693).
TOKEN_EXCHANGE = "token_exchange"
#: Identity provider errors that the user resolves by signing in again: MFA
#: or conditional access (Entra adds a claims challenge), or consent.
_INTERACTION_ERRORS = frozenset({"interaction_required", "login_required", "consent_required"})
#: Entra marks an ``invalid_grant`` the user can resolve by signing in again
#: with a ``suberror`` (``basic_action``, ``additional_action``,
#: ``consent_required``, ...), as Microsoft's client libraries read it; these
#: two it cannot resolve. Without a suberror, ``invalid_grant`` is a refusal.
_REFUSAL_SUBERRORS = frozenset({"client_mismatch", "protection_policy_required"})


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
    """The warehouse's token service refused to issue a token for this user.

    ``error`` is the OAuth error code it answered with, when it sent one.
    """

    code = "DATA_SOURCE_SIGN_IN_REFUSED"
    status = 403

    def __init__(self, message: str, error: str | None = None) -> None:
        super().__init__(message)
        self.error = error


class BiTokenSignInEndedError(DataSourceSignInError):
    """A BI token's warehouse sign-in is over: the identity provider no longer honours it.

    Revoked there, the user disabled or gone, unused for too long, or a sign-in
    policy that wants the user at the sign-in page again. A new BI token
    signs in anew.
    """

    code = "BI_TOKEN_SIGN_IN_ENDED"
    status = 401


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


def any_source_logs_in_as_user() -> bool:
    """Whether a configured data source logs in as the user (``DB_<NAME>_AUTH=user``)."""
    try:
        from ob_flight.datasources import DataSourceConfigError, configured_datasources
    except ImportError:  # no ob-flight-extension: no named data sources
        return False
    try:
        return any(logs_in_as_user(source) for source in configured_datasources())
    except DataSourceConfigError:
        return False


def bi_token_sign_in_available() -> bool:
    """Whether a new BI token gets a warehouse sign-in: a source needs one, and it can be made."""
    return not warehouse_sign_in_missing() and any_source_logs_in_as_user()


@dataclass(frozen=True)
class _Caller:
    """Who a query on a ``user`` source runs as, and what stands for their sign-in."""

    sub: str
    #: The identity provider token the request came with; else None, and
    credential: str | None
    #: the BI token whose warehouse sign-in stands in for it.
    token_id: str | None = None


def _caller() -> _Caller:
    """The signed-in caller. Raises SignInRequiredError."""
    principal = current_principal()
    credential = current_credential()
    if principal is not None and principal.sub is not None:
        if credential:
            return _Caller(principal.sub, credential)
        if principal.token_id is not None and _holds_sign_in(principal.token_id):
            return _Caller(principal.sub, None, principal.token_id)
    if principal is not None and principal.token_id is not None:
        missing = warehouse_sign_in_missing()
        if missing:
            raise SignInRequiredError(
                "This data source logs in as the signed-in user, and BI tokens on this "
                f"server hold no warehouse sign-in (it needs {', '.join(missing)}): call "
                "with the identity provider's access token."
            )
        raise SignInRequiredError(
            "This data source logs in as the signed-in user, and this BI token holds no "
            "warehouse sign-in: create a new BI token on the /tokens page."
        )
    raise SignInRequiredError(
        "This data source logs in as the signed-in user; call with the identity "
        "provider's access token (AUTH_MODE=oidc) or a BI token, not an API key."
    )


def _holds_sign_in(token_id: str) -> bool:
    with _tokens_lock:
        if token_id in _minted:
            return True
    store = get_token_store()
    return store is not None and store.has_sign_in(token_id)


def caller_scope() -> str:
    """The user part of a cache scope: a hash of the caller, never the raw ``sub``."""
    return hashlib.sha256(f"sub:{_caller().sub}".encode()).hexdigest()[:32]


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
#: A BI token's owner's identity provider token, minted from its warehouse
#: sign-in, by token id; and one lock per BI token, so its refresh token is
#: redeemed (and a rotated one stored) once at a time. The locks are kept for
#: the process's life, one per BI token used: a lock dropped while a refresh
#: holds it would let the next query make a new one and redeem the same
#: refresh token again, which ends the sign-in where the IdP rotates them.
_minted: dict[str, _Issued] = {}
_sign_in_locks: dict[str, threading.Lock] = {}
_tokens_lock = threading.Lock()


def reset_tokens() -> None:
    """Forget every exchanged token (tests, shutdown)."""
    with _tokens_lock:
        _tokens.clear()
        _locks.clear()
        _minted.clear()
        _sign_in_locks.clear()


@dataclass(frozen=True)
class UserLogin:
    """How to connect to a source as the caller, and until when that connection may serve."""

    owner: str  # a hash of the caller (caller_scope), never the raw ``sub``
    overrides: dict[str, Any]
    usable_until: float  # monotonic: a minute before the warehouse token expires


def login_as_caller(source: Any) -> UserLogin:
    """The ``connect()`` arguments that log ``source`` in as the caller, and their lifetime."""
    issued = _database_login(source)
    return UserLogin(
        owner=caller_scope(),
        overrides=_login_overrides(source, issued.token),
        usable_until=issued.expires_at - _RENEW_MARGIN_SECONDS,
    )


def database_token(source: Any) -> str:
    """A token for ``source``'s warehouse, issued for the caller.

    Reused until a minute before it expires, then exchanged again from the
    current request's token. One exchange at a time per (source, user).
    """
    return _database_login(source).token


def _database_login(source: Any) -> _Issued:
    caller = _caller()
    key = (source.fingerprint(), caller.sub)
    with _tokens_lock:
        issued = _tokens.get(key)
        lock = _locks.setdefault(key, threading.Lock())
    if issued is not None and issued.expires_at - _RENEW_MARGIN_SECONDS > time.monotonic():
        return issued
    with lock:
        with _tokens_lock:
            issued = _tokens.get(key)
        if issued is not None and issued.expires_at - _RENEW_MARGIN_SECONDS > time.monotonic():
            return issued
        token, expires_in = _exchange_as(source, caller)
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
        return issued


def connect_overrides(source: Any) -> dict[str, Any]:
    """The ``connect()`` arguments that log ``source`` in as the caller."""
    return _login_overrides(source, database_token(source))


def _login_overrides(source: Any, token: str) -> dict[str, Any]:
    if source.dialect in ("databricks", "bigquery"):
        # BigQuery: the token in place of a service account key file.
        overrides: dict[str, Any] = {"access_token": token}
        if source.dialect == "bigquery":
            overrides["credentials_file"] = None
        return overrides
    if source.dialect == "snowflake":
        # The token says who connects; a service user or password configured
        # for the source does not go along.
        return {"authenticator": "oauth", "token": token, "user": None, "password": None}
    raise SignInRefusedError(  # configured_datasources refuses this; defence in depth
        f"Data source '{source.name}' ({source.dialect}) cannot log in as the user."
    )


def _exchange_as(source: Any, caller: _Caller) -> tuple[str, float]:
    """A warehouse token for ``caller``: from their request's token, or their BI token's."""
    if caller.credential is not None:
        return _exchange(source, caller.credential)
    token_id = caller.token_id or ""  # _caller sets one of the two
    if source.dialect == "snowflake" and source.oauth_exchange() == ENTRA_OBO:
        # Entra's refresh token is bound to the user and OBSL's client, not to
        # one resource: redeemed for the warehouse's scope, it yields the
        # warehouse token itself, with no on-behalf-of step.
        with _sign_in_lock(token_id):
            return _redeem_sign_in(token_id, source.oauth_scope() or "", source)
    return _exchange(source, _bi_token_user_token(token_id, source))


def _sign_in_lock(token_id: str) -> threading.Lock:
    with _tokens_lock:
        return _sign_in_locks.setdefault(token_id, threading.Lock())


def _bi_token_user_token(token_id: str, source: Any) -> str:
    """The BI token's owner's identity provider token, minted from its warehouse sign-in."""
    lock = _sign_in_lock(token_id)
    with _tokens_lock:
        issued = _minted.get(token_id)
    if issued is not None and issued.expires_at - _RENEW_MARGIN_SECONDS > time.monotonic():
        return issued.token
    with lock:
        with _tokens_lock:
            issued = _minted.get(token_id)
        if issued is not None and issued.expires_at - _RENEW_MARGIN_SECONDS > time.monotonic():
            return issued.token
        token, expires_in = _redeem_sign_in(token_id, get_consent_scopes(), source)
        issued = _Issued(token, time.monotonic() + expires_in)
        with _tokens_lock:
            now = time.monotonic()
            for stale in [k for k, v in _minted.items() if v.expires_at <= now]:
                del _minted[stale]  # its lock stays: a refresh may hold it right now
            _minted[token_id] = issued
        return token


def _redeem_sign_in(token_id: str, scope: str, source: Any) -> tuple[str, float]:
    """Redeem the BI token's refresh token at the identity provider. Holds its sign-in lock.

    A refresh token the identity provider rotates is stored before the new
    token is used: some (Okta) end the whole sign-in when an old one comes
    back. One it refuses (``invalid_grant``, or a sign-in the user must go
    through again) is dropped: the BI token's sign-in has ended.
    """
    store = get_token_store()
    client, endpoint = _client_at_idp(source)
    refresh_token = store.refresh_token(token_id) if store is not None else None
    if store is None or refresh_token is None:
        raise BiTokenSignInEndedError(
            "This BI token's warehouse sign-in has ended: create a new BI token on the "
            "/tokens page."
        )
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if scope:
        form["scope"] = scope
    credentials = (quote(client.client_id, safe=""), quote(client.client_secret, safe=""))
    try:
        body = _token_response(source, endpoint, form, auth=credentials)
    except (SignInRefusedError, SignInInteractionRequiredError) as exc:
        if isinstance(exc, SignInRefusedError) and exc.error != "invalid_grant":
            raise  # OBSL's client or its scope, not the user's sign-in
        store.drop_refresh_token(token_id)
        with _tokens_lock:
            _minted.pop(token_id, None)
        raise BiTokenSignInEndedError(
            f"{exc.message} This BI token's warehouse sign-in has ended: create a new BI "
            "token on the /tokens page."
        ) from None
    rotated = body.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != refresh_token:
        store.replace_refresh_token(token_id, rotated)
    return _issued_token(source, body)


def _exchange(source: Any, subject_token: str) -> tuple[str, float]:
    if source.dialect == "databricks":
        return _exchange_databricks(source, subject_token)
    if source.dialect == "bigquery":
        return _exchange_google_sts(source, subject_token)
    if source.dialect == "snowflake" and source.oauth_exchange() == ENTRA_OBO:
        return _exchange_entra_obo(source, subject_token)
    if source.dialect == "snowflake" and source.oauth_exchange() == TOKEN_EXCHANGE:
        return _exchange_at_idp(source, subject_token)
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


def _exchange_google_sts(source: Any, subject_token: str) -> tuple[str, float]:
    """Workforce identity federation: the user's token for a Google access token.

    Google's security token service checks the token against the workforce
    pool provider (``DB_<NAME>_WORKFORCE_PROVIDER``): its issuer and keys, and
    that the token's ``aud`` is the provider's client ID. The exchange is
    billed to ``DB_<NAME>_WORKFORCE_USER_PROJECT``, else the source's project.
    No client authenticates: no OBSL secret is involved.
    """
    form = {
        "grant_type": _TOKEN_EXCHANGE,
        "audience": source.workforce_provider() or "",
        "scope": _CLOUD_PLATFORM_SCOPE,
        "requested_token_type": _ACCESS_TOKEN_TYPE,
        "subject_token": subject_token,
        "subject_token_type": _ID_TOKEN_TYPE,
        "options": json.dumps({"userProject": source.workforce_user_project() or ""}),
    }
    return _post_token_request(source, _GOOGLE_STS, form)


def _exchange_entra_obo(source: Any, subject_token: str) -> tuple[str, float]:
    """Entra ID's on-behalf-of flow: the user's token for one issued for the warehouse.

    OBSL asks as its own confidential client, the one the user's token was
    issued for (``OIDC_AUDIENCE``), for ``DB_<NAME>_OAUTH_SCOPE``: for
    Snowflake the warehouse's app ID URI and the role scope, such as
    ``api://<Snowflake app ID>/session:role-any``.
    """
    client, endpoint = _client_at_idp(source)
    form = {
        "grant_type": _JWT_BEARER,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "assertion": subject_token,
        "scope": source.oauth_scope() or "",
        "requested_token_use": "on_behalf_of",
    }
    return _post_token_request(source, endpoint, form)


def _exchange_at_idp(source: Any, subject_token: str) -> tuple[str, float]:
    """The standard token exchange (RFC 8693) at the identity provider: Okta, Keycloak.

    OBSL asks as its own confidential client, which the user's token must be
    issued for (``OIDC_AUDIENCE``), for ``DB_<NAME>_OAUTH_SCOPE`` and, when set,
    ``DB_<NAME>_OAUTH_AUDIENCE`` (Okta: the target authorization server's
    audience; Keycloak: the target client). The client authenticates with
    HTTP Basic, its id and secret form-encoded first (RFC 6749, 2.3.1): the
    method every authorization server must accept, and what both document.
    """
    client, endpoint = _client_at_idp(source)
    form = {
        "grant_type": _TOKEN_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": _ACCESS_TOKEN_TYPE,
        "scope": source.oauth_scope() or "",
    }
    audience = source.oauth_audience()
    if audience:
        form["audience"] = audience
    credentials = (quote(client.client_id, safe=""), quote(client.client_secret, safe=""))
    return _post_token_request(source, endpoint, form, auth=credentials)


def _client_at_idp(source: Any) -> tuple[OidcClient, str]:
    """OBSL's own client at the identity provider, and its token endpoint."""
    client = get_oidc_client()
    endpoint = get_token_endpoint()
    if client is None or endpoint is None:  # startup refuses this; defence in depth
        raise SignInUnavailableError(
            f"Data source '{source.name}' logs in as the user through the identity "
            "provider, and OBSL has no client there (OIDC_CLIENT_ID) or no token endpoint."
        )
    return client, endpoint


def _needs_sign_in(body: Mapping[str, Any]) -> bool:
    """Whether an error answer is one the user resolves by signing in again."""
    if body.get("error") in _INTERACTION_ERRORS:
        return True
    suberror = body.get("suberror")
    return (
        body.get("error") == "invalid_grant"
        and isinstance(suberror, str)
        and bool(suberror)
        and suberror not in _REFUSAL_SUBERRORS
    )


def _post_token_request(
    source: Any, url: str, form: Mapping[str, str], auth: tuple[str, str] | None = None
) -> tuple[str, float]:
    """POST a token request; return (access token, seconds valid). Never logs a token.

    ``auth`` is the client's HTTP Basic credentials, for a client that
    authenticates that way.
    """
    return _issued_token(source, _token_response(source, url, form, auth=auth))


def _token_response(
    source: Any, url: str, form: Mapping[str, str], auth: tuple[str, str] | None = None
) -> dict[str, Any]:
    """POST a token request; its answer. Raises the sign-in errors. Never logs a token."""
    try:
        response = httpx.post(url, data=dict(form), auth=auth, timeout=_EXCHANGE_TIMEOUT_SECONDS)
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
        if isinstance(body, dict) and _needs_sign_in(body):
            claims = body.get("claims")
            raise SignInInteractionRequiredError(
                f"Data source '{source.name}' needs the user to sign in again first ({detail}).",
                claims=claims if isinstance(claims, str) and claims else None,
            )
        raise SignInRefusedError(
            f"Data source '{source.name}' refused to log in this user ({detail}).",
            error=body.get("error") if isinstance(body, dict) else None,
        )
    return body if isinstance(body, dict) else {}


def _issued_token(source: Any, body: Mapping[str, Any]) -> tuple[str, float]:
    """The access token a token service answered with, and for how many seconds."""
    token = body.get("access_token")
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
