"""Authentication handlers for the Flight SQL server."""

from __future__ import annotations

import os
from typing import Any

import pyarrow.flight as flight


class NoopAuthHandler(flight.ServerAuthHandler):  # type: ignore[misc]
    """No authentication — accept all connections."""

    def authenticate(self, outgoing: Any, incoming: Any) -> None:
        """Accept all connections without authentication."""

    def is_valid(self, token: bytes) -> str:
        """All tokens are valid — return empty peer identity."""
        return ""


class TokenAuthHandler(flight.ServerAuthHandler):  # type: ignore[misc]
    """Simple static bearer token authentication."""

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


def create_auth_handler() -> flight.ServerAuthHandler:
    """Create an auth handler based on environment variables.

    FLIGHT_AUTH_MODE=none (default) -> NoopAuthHandler
    FLIGHT_AUTH_MODE=token -> TokenAuthHandler(FLIGHT_API_TOKEN)
    """
    mode = os.getenv("FLIGHT_AUTH_MODE", "none").lower()
    if mode == "token":
        token = os.getenv("FLIGHT_API_TOKEN", "")
        if not token:
            raise ValueError("FLIGHT_AUTH_MODE=token requires FLIGHT_API_TOKEN to be set")
        return TokenAuthHandler(token)
    return NoopAuthHandler()
