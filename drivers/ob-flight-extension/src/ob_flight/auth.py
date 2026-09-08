"""Authentication handlers for the Flight SQL server.

When OBSL's shared auth subsystem is in ``api_key`` mode, the Flight server
validates the API key against the shared key store two ways, because clients
differ on how they send it:

* :class:`AuthMiddlewareFactory` reads it from the call headers. This is the
  path every current client takes - see the long note below.
* :class:`SharedKeyAuthHandler` answers Flight's legacy ``Handshake``, kept for
  anything that still speaks it.

The legacy ``FLIGHT_AUTH_MODE=token`` / ``FLIGHT_API_TOKEN`` path still works
for one release with a deprecation warning. See design/PLAN_authentication.md
§3.2.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from pyarrow import flight

logger = logging.getLogger("ob_flight.auth")


def _decode_token(token: Any) -> str:
    """Coerce a handshake token (bytes or str) to a plain string."""
    if isinstance(token, bytes):
        return token.decode("utf-8", errors="replace")
    return str(token)


class NoopAuthHandler(flight.ServerAuthHandler):  # type: ignore[misc]
    """No authentication — accept all connections."""

    def authenticate(self, outgoing: Any, incoming: Any) -> None:
        """Accept all connections without authentication."""

    def is_valid(self, token: bytes) -> str:
        """All tokens are valid — return empty peer identity."""
        return ""


class TokenAuthHandler(flight.ServerAuthHandler):  # type: ignore[misc]
    """Simple static bearer token authentication (legacy FLIGHT_API_TOKEN)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token.encode("utf-8")

    def authenticate(self, outgoing: Any, incoming: Any) -> None:
        """Validate the token from the client."""
        buf = incoming.read()
        if buf != self._token:
            raise flight.FlightUnauthenticatedError("Invalid token")
        outgoing.write(self._token)

    def is_valid(self, token: bytes) -> str:
        """Check if the token matches."""
        if token != self._token:
            raise flight.FlightUnauthenticatedError("Invalid token")
        return "authenticated"


class SharedKeyAuthHandler(flight.ServerAuthHandler):  # type: ignore[misc]
    """Validate the handshake credential against OBSL's shared key store.

    The client sends its API key as the Basic-auth password (the username is
    ignored) or as the raw handshake token; ``validate_fn`` returns whether
    it matches a configured key. On success the echoed token is reused as the
    session token, which ``is_valid`` re-checks on every subsequent call.
    """

    def __init__(
        self, validate_fn: Callable[[str], bool], *, header_auth_installed: bool = False
    ) -> None:
        super().__init__()
        self._validate = validate_fn
        self._header_auth_installed = header_auth_installed

    def authenticate(self, outgoing: Any, incoming: Any) -> None:
        # Two protocols arrive here. The legacy Handshake puts the key on the
        # stream. AuthenticateBasicToken - what every current client speaks -
        # reuses the same RPC but puts the key in a header and sends nothing,
        # so this read fails with "Stream is closed". That error was the whole
        # symptom: it is not a broken client, it is a client speaking the
        # newer of the two protocols this RPC carries.
        try:
            token = incoming.read()
        except OSError:
            token = b""
        if not token:
            if self._header_auth_installed:
                # The middleware read the header, validated it, and will return
                # the bearer token in the response. Nothing to do here.
                return
            raise flight.FlightUnauthenticatedError(
                "No API key on the handshake stream. This server accepts the "
                "legacy handshake only; enable api_key mode for header auth."
            )
        if not self._validate(_decode_token(token)):
            raise flight.FlightUnauthenticatedError("Invalid API key")
        outgoing.write(token)

    def is_valid(self, token: bytes) -> str:
        if not token and self._header_auth_installed:
            # A client that authenticated by header never handshook, so there
            # is no token here to check. Rejecting it would veto every current
            # client - which is exactly what this server did. The middleware
            # has already accepted or refused this call; say nothing and let
            # its answer stand. Never reachable unless that middleware is
            # installed: ``build_api_key_auth`` is the only way to set this,
            # and it builds the pair together.
            return ""
        if not self._validate(_decode_token(token)):
            raise flight.FlightUnauthenticatedError("Invalid API key")
        return "authenticated"


def create_auth_handler(
    validate_fn: Callable[[str], bool] | None = None,
) -> flight.ServerAuthHandler:
    """Create an auth handler for the Flight server.

    Priority:
    1. ``validate_fn`` supplied (shared auth in api_key mode) → SharedKeyAuthHandler.
    2. ``FLIGHT_AUTH_MODE=token`` (legacy) → TokenAuthHandler + deprecation warning.
    3. Otherwise → NoopAuthHandler (no auth).
    """
    if validate_fn is not None:
        return SharedKeyAuthHandler(validate_fn)

    mode = os.getenv("FLIGHT_AUTH_MODE", "none").lower()
    if mode == "token":
        token = os.getenv("FLIGHT_API_TOKEN", "")
        if not token:
            raise ValueError("FLIGHT_AUTH_MODE=token requires FLIGHT_API_TOKEN to be set")
        logger.warning(
            "FLIGHT_AUTH_MODE=token / FLIGHT_API_TOKEN is deprecated and will be removed "
            "in a future release. Migrate to AUTH_MODE=api_key + API_KEYS (one shared key "
            "store across REST, Flight, and pgwire). See design/PLAN_authentication.md."
        )
        return TokenAuthHandler(token)
    return NoopAuthHandler()


# ---------------------------------------------------------------------------
# Header-based authentication (what ADBC actually speaks)
# ---------------------------------------------------------------------------
#
# ``ServerAuthHandler`` implements Flight's *legacy* ``Handshake``. No current
# client uses it: ADBC and pyarrow's own ``authenticate_basic_token`` both call
# ``AuthenticateBasicToken``, the Flight SQL standard, which carries the
# credential in an ``authorization`` header and expects the *response* to carry
# the issued bearer token in one. A handshake handler can do neither - it reads
# a stream that this RPC does not open, and writes to a stream the client does
# not read - so with ``AUTH_MODE=api_key`` every ADBC connection failed with
# "Stream is closed" whether the key was right or wrong.
#
# Middleware sees the headers on every call and can set them on the way out,
# which is the whole protocol. Three spellings are accepted, and they are the
# three a client actually sends:
#
#     authorization: Basic  <base64(user:key)>   ADBC username/password
#     authorization: Bearer <key>                ADBC authorization_header
#     x-api-key: <key>                           the header REST uses
#
# The username in the Basic form is ignored, as it was by the handshake
# handler: OBSL has API keys, not accounts.

AUTH_MIDDLEWARE_KEY = "obsl_auth"

_BEARER_PREFIX = "bearer "
_BASIC_PREFIX = "basic "

#: Where pyarrow puts the token a legacy handshake issued.
_HANDSHAKE_TOKEN_HEADER = "auth-token-bin"


def _is_handshake(info: Any) -> bool:
    """Whether this call is the Handshake RPC itself."""
    try:
        return bool(getattr(info, "method", None) == flight.FlightMethod.HANDSHAKE)
    except AttributeError:  # pragma: no cover - older pyarrow without the enum
        return False


def _credential_from_headers(headers: Mapping[str, Any]) -> str | None:
    """The API key a call carries, in whichever of the three forms it used."""
    lowered: dict[str, Any] = {str(k).lower(): v for k, v in headers.items()}

    def first(name: str) -> str | None:
        value = lowered.get(name)
        if value is None:
            return None
        if isinstance(value, list | tuple):
            value = value[0] if value else None
        if value is None:
            return None
        return _decode_token(value)

    api_key = first("x-api-key")
    if api_key:
        return api_key

    # What a legacy handshake issues, sent on every call after it. The token
    # is the key itself (``authenticate`` echoes it back), so it validates the
    # same way - without this, a handshake would succeed and every call after
    # it would be refused.
    handshake_token = first(_HANDSHAKE_TOKEN_HEADER)
    if handshake_token:
        return handshake_token

    authorization = first("authorization")
    if not authorization:
        return None
    if authorization.lower().startswith(_BEARER_PREFIX):
        return authorization[len(_BEARER_PREFIX) :].strip()
    if authorization.lower().startswith(_BASIC_PREFIX):
        encoded = authorization[len(_BASIC_PREFIX) :].strip()
        # ADBC sends this unpadded, which ``b64decode`` rejects outright - so
        # a correct key looked like no key at all, and the client was told to
        # send one it had already sent. Padding is re-added rather than the
        # error tolerated: an undecodable credential and an absent one deserve
        # different answers.
        encoded += "=" * (-len(encoded) % 4)
        try:
            decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            return None
        # ``user:key`` - the username is ignored; OBSL has keys, not accounts.
        _, _, key = decoded.partition(":")
        return key or None
    return None


class _AuthMiddleware(flight.ServerMiddleware):  # type: ignore[misc]
    """Carries the issued bearer token back to the client."""

    def __init__(self, token: str | None) -> None:
        super().__init__()
        self._token = token

    def sending_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        # ``AuthenticateBasicToken`` answers with the token to use from here
        # on. Echoing the validated key keeps one secret rather than minting a
        # second one with its own lifetime - the client already holds it, and
        # `Bearer <key>` is a form this same middleware accepts.
        return {"authorization": f"Bearer {self._token}"}


class AuthMiddlewareFactory(flight.ServerMiddlewareFactory):  # type: ignore[misc]
    """Reject any call that does not carry a valid API key.

    Enforced per call rather than once per connection, because that is what
    the protocol gives us: a Flight client may open several streams, and a
    ticket issued to one caller must not be redeemable by another.
    """

    def __init__(
        self, validate_fn: Callable[[str], bool], *, handshake_handler_installed: bool = False
    ) -> None:
        super().__init__()
        self._validate = validate_fn
        self._handshake_handler_installed = handshake_handler_installed

    def start_call(self, info: Any, headers: Mapping[str, Any]) -> Any:
        credential = _credential_from_headers(headers)
        if credential is None:
            if self._handshake_handler_installed and _is_handshake(info):
                # A handshake carrying no header is the legacy protocol, where
                # the key travels on the stream - so there is nothing to check
                # here yet, and the ServerAuthHandler refuses it if it is
                # wrong. Only this one credential-less call is let through: a
                # handshake that *does* carry a header is AuthenticateBasicToken
                # and is validated below like any other call.
                return None
            raise flight.FlightUnauthenticatedError(
                "Missing API key. Send it as 'authorization: Bearer <key>', "
                "'x-api-key: <key>', or the password of a Basic credential."
            )
        if not self._validate(credential):
            raise flight.FlightUnauthenticatedError("Invalid API key")
        return _AuthMiddleware(credential)


def build_api_key_auth(
    validate_fn: Callable[[str], bool] | None = None,
) -> tuple[SharedKeyAuthHandler, dict[str, Any]]:
    """The handler and middleware for ``api_key`` mode, built as one pair.

    They are returned together because neither is correct alone here. The
    handler must not veto calls that carry no handshake token, and the
    middleware must not refuse the Handshake RPC - each of those concessions
    is safe only because the other half is installed to cover it. Handing back
    a tuple is what stops the two from drifting apart in the caller, which is
    how the production wiring came to refuse every client while the parts
    passed their own tests.
    """
    if validate_fn is None:
        # Imported lazily so this module stays importable when ``orionbelt``
        # is not installed (standalone Flight use).
        from orionbelt.auth import validate_credential

        validate_fn = validate_credential

    handler = SharedKeyAuthHandler(validate_fn, header_auth_installed=True)
    middleware = {
        AUTH_MIDDLEWARE_KEY: AuthMiddlewareFactory(validate_fn, handshake_handler_installed=True)
    }
    return handler, middleware
