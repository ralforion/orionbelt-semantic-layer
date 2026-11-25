"""OBSL's one sign-in that gives a new BI token its warehouse sign-in.

A data source that logs in as the user (``DB_<NAME>_AUTH=user``) needs the
user's identity provider token, and a BI tool holds a BI token, which is not
one. So when a user creates a BI token on ``/tokens``, OBSL signs them in once
more, as its own confidential client (``OIDC_CLIENT_ID``), asking for
``offline_access``: the identity provider hands a refresh token to OBSL's
server, the new BI token keeps it (encrypted), and OBSL redeems it for the
user's token whenever the BI tool queries such a source. See
design/PLAN_bi_tokens_user_sources.md.

The flow: the page asks for a sign-in (:meth:`Consents.start`) and opens its
``authorize_url`` in a popup; the identity provider sends the popup back to
``/tokens/consent``, which redeems the code (:meth:`Consents.finish`); the
page polls :meth:`Consents.status`, then creates the BI token with the
sign-in's id, which hands its refresh token over (:meth:`Consents.take`). A
sign-in under way lives in this process's memory only, for ten minutes, and
is used once.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, urlencode

import httpx

from orionbelt.auth.config import (
    get_audience,
    get_authorization_endpoint,
    get_consent_scopes,
    get_oidc_client,
    get_token_endpoint,
    verify_access_token,
)
from orionbelt.auth.errors import TokenInvalidError

ConsentState = Literal["pending", "finished", "failed"]
PENDING: ConsentState = "pending"
FINISHED: ConsentState = "finished"
FAILED: ConsentState = "failed"

#: How long a sign-in may take, from the page asking to the token's creation.
LIFETIME_SECONDS = 600.0
#: Sign-ins under way at once; past it the oldest is dropped.
_MAX_UNDER_WAY = 1000
_TIMEOUT_SECONDS = 10.0
_UNKNOWN = "This sign-in is unknown or has expired: create the token again on /tokens."


class ConsentError(Exception):
    """The sign-in cannot go on. Carries an HTTP status and a code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass
class _SignIn:
    id: str
    sub: str
    state: str
    redirect_uri: str
    started: float  # monotonic
    verifier: str = field(repr=False)
    outcome: ConsentState = PENDING
    message: str = ""
    refresh_token: str | None = field(default=None, repr=False)


def _challenge(verifier: str) -> str:
    """The PKCE S256 challenge of ``verifier`` (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class Consents:
    """The sign-ins under way, by id and by the ``state`` the identity provider echoes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: OrderedDict[str, _SignIn] = OrderedDict()
        self._by_state: dict[str, str] = {}

    def start(self, sub: str, redirect_uri: str) -> tuple[str, str]:
        """A new sign-in for ``sub``: its id, and where the user signs in.

        Raises:
            ConsentError: OBSL has no client at the identity provider, or does
                not know where users sign in.
        """
        client = get_oidc_client()
        endpoint = get_authorization_endpoint()
        if client is None or endpoint is None:
            raise ConsentError(
                404, "WAREHOUSE_SIGN_IN_OFF", "Warehouse sign-in is not enabled on this server."
            )
        consent_id = secrets.token_urlsafe(18)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        query = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "scope": get_consent_scopes(),
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
        # Some identity providers (Auth0) issue a token for the API only when asked.
        if get_audience():
            query["audience"] = get_audience()
        sign_in = _SignIn(
            id=consent_id,
            sub=sub,
            state=state,
            redirect_uri=redirect_uri,
            started=time.monotonic(),
            verifier=verifier,
        )
        with self._lock:
            self._purge()
            while len(self._by_id) >= _MAX_UNDER_WAY:
                _, oldest = self._by_id.popitem(last=False)
                self._by_state.pop(oldest.state, None)
            self._by_id[consent_id] = sign_in
            self._by_state[state] = consent_id
        separator = "&" if "?" in endpoint else "?"
        return consent_id, f"{endpoint}{separator}{urlencode(query, quote_via=quote)}"

    def finish(self, state: str, code: str) -> str:
        """Redeem the code the identity provider sent back; the sign-in's id.

        Raises:
            ConsentError: No sign-in under way has this ``state``, the identity
                provider refused the code or issued no refresh token, or it
                signed in another user than the one who started.
        """
        sign_in = self._pending(state)
        try:
            refresh_token = _redeem(code, sign_in)
        except ConsentError as exc:
            with self._lock:
                sign_in.outcome = FAILED
                sign_in.message = exc.message
            raise
        with self._lock:
            sign_in.outcome = FINISHED
            sign_in.refresh_token = refresh_token
        return sign_in.id

    def fail(self, state: str, message: str) -> None:
        """The identity provider sent the user back with an error (cancelled, refused)."""
        try:
            sign_in = self._pending(state)
        except ConsentError:
            return
        with self._lock:
            sign_in.outcome = FAILED
            sign_in.message = message

    def status(self, consent_id: str, sub: str) -> tuple[ConsentState, str]:
        """``pending``, ``finished`` or ``failed``, and why it failed.

        Raises:
            ConsentError: No sign-in of ``sub`` has this id (also once it expired).
        """
        with self._lock:
            self._purge()
            sign_in = self._by_id.get(consent_id)
            if sign_in is None or sign_in.sub != sub:
                raise ConsentError(404, "CONSENT_NOT_FOUND", _UNKNOWN)
            return sign_in.outcome, sign_in.message

    def take(self, consent_id: str, sub: str) -> str:
        """The refresh token of ``sub``'s finished sign-in, once.

        Raises:
            ConsentError: No finished sign-in of ``sub`` has this id.
        """
        with self._lock:
            self._purge()
            sign_in = self._by_id.get(consent_id)
            if (
                sign_in is None
                or sign_in.sub != sub
                or sign_in.outcome != FINISHED
                or sign_in.refresh_token is None
            ):
                raise ConsentError(422, "CONSENT_NOT_FOUND", _UNKNOWN)
            del self._by_id[consent_id]
            return sign_in.refresh_token

    def clear(self) -> None:
        """Forget every sign-in under way (tests)."""
        with self._lock:
            self._by_id.clear()
            self._by_state.clear()

    def _pending(self, state: str) -> _SignIn:
        with self._lock:
            self._purge()
            consent_id = self._by_state.pop(state, None)
            sign_in = self._by_id.get(consent_id) if consent_id is not None else None
        if sign_in is None or sign_in.outcome != PENDING:
            raise ConsentError(400, "CONSENT_NOT_FOUND", _UNKNOWN)
        return sign_in

    def _purge(self) -> None:
        """Drop sign-ins older than :data:`LIFETIME_SECONDS` (oldest first). Holds the lock."""
        cutoff = time.monotonic() - LIFETIME_SECONDS
        while self._by_id:
            consent_id, oldest = next(iter(self._by_id.items()))
            if oldest.started > cutoff:
                break
            del self._by_id[consent_id]
            self._by_state.pop(oldest.state, None)


def _redeem(code: str, sign_in: _SignIn) -> str:
    """Exchange the code as OBSL's client; the refresh token, for the user who started."""
    client = get_oidc_client()
    endpoint = get_token_endpoint()
    if client is None or endpoint is None:
        raise ConsentError(
            404, "WAREHOUSE_SIGN_IN_OFF", "Warehouse sign-in is not enabled on this server."
        )
    try:
        response = httpx.post(
            endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": sign_in.redirect_uri,
                "code_verifier": sign_in.verifier,
            },
            auth=(quote(client.client_id, safe=""), quote(client.client_secret, safe="")),
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ConsentError(
            503,
            "CONSENT_UNAVAILABLE",
            f"Could not reach the identity provider ({type(exc).__name__}).",
        ) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 400:
        error = body.get("error", f"HTTP {response.status_code}")
        description = body.get("error_description", "")
        detail = f"{error}: {description}" if description else str(error)
        raise ConsentError(
            502,
            "CONSENT_REFUSED",
            f"The identity provider did not complete the sign-in ({detail}).",
        )
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsentError(
            502, "CONSENT_REFUSED", "The identity provider completed the sign-in without a token."
        )
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsentError(
            502,
            "CONSENT_NO_REFRESH_TOKEN",
            "The identity provider issued no refresh token: allow offline_access for "
            "OrionBelt's client (OIDC_CLIENT_ID), and ask for it (OIDC_CONSENT_SCOPES).",
        )
    try:
        principal = verify_access_token(access_token)
    except TokenInvalidError as exc:
        raise ConsentError(
            502,
            "CONSENT_TOKEN_INVALID",
            f"The identity provider's token for OrionBelt's client does not verify "
            f"({exc.message}); it must be issued for OIDC_AUDIENCE.",
        ) from None
    if principal.sub != sign_in.sub:
        raise ConsentError(
            403,
            "CONSENT_WRONG_USER",
            "The identity provider signed in another user than the one creating the token.",
        )
    return refresh_token


#: The sign-ins under way in this process.
consents = Consents()
