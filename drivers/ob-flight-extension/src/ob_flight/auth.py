"""Authentication handlers for the Flight SQL server.

When OBSL's shared auth subsystem is in ``api_key`` mode, the Flight server
validates the handshake credential (the API key, sent as the Basic-auth
password or handshake token) against the shared key store via
:class:`SharedKeyAuthHandler`. The legacy ``FLIGHT_AUTH_MODE=token`` /
``FLIGHT_API_TOKEN`` path still works for one release with a deprecation
warning. See design/PLAN_authentication.md §3.2.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import pyarrow.flight as flight

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

    def __init__(self, validate_fn: Callable[[str], bool]) -> None:
        super().__init__()
        self._validate = validate_fn

    def authenticate(self, outgoing: Any, incoming: Any) -> None:
        token = incoming.read()
        if not self._validate(_decode_token(token)):
            raise flight.FlightUnauthenticatedError("Invalid API key")
        outgoing.write(token)

    def is_valid(self, token: bytes) -> str:
        if not self._validate(_decode_token(token)):
            raise flight.FlightUnauthenticatedError("Invalid API key")
        return "authenticated"


def build_shared_key_handler() -> SharedKeyAuthHandler:
    """Build a handler bound to OBSL's shared credential validator.

    Imported lazily so this module stays importable when ``orionbelt`` is not
    installed (standalone Flight use). In practice the Flight server runs
    in-process with the OBSL API, so the import resolves.
    """
    from orionbelt.auth import validate_credential

    return SharedKeyAuthHandler(validate_credential)


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
