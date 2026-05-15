"""Tests for Flight authentication handlers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow.flight as flight
import pytest

from ob_flight.auth import NoopAuthHandler, TokenAuthHandler, create_auth_handler


class TestNoopAuthHandler:
    def test_authenticate(self):
        handler = NoopAuthHandler()
        handler.authenticate(MagicMock(), MagicMock())  # should not raise

    def test_is_valid(self):
        handler = NoopAuthHandler()
        result = handler.is_valid(b"anything")
        assert result == ""


class TestTokenAuthHandler:
    def test_authenticate_success(self):
        handler = TokenAuthHandler("secret")
        incoming = MagicMock()
        incoming.read.return_value = b"secret"
        outgoing = MagicMock()
        handler.authenticate(outgoing, incoming)
        outgoing.write.assert_called_once_with(b"secret")

    def test_authenticate_failure(self):
        handler = TokenAuthHandler("secret")
        incoming = MagicMock()
        incoming.read.return_value = b"wrong"
        with pytest.raises(flight.FlightUnauthenticatedError):
            handler.authenticate(MagicMock(), incoming)

    def test_is_valid_success(self):
        handler = TokenAuthHandler("secret")
        assert handler.is_valid(b"secret") == "authenticated"

    def test_is_valid_failure(self):
        handler = TokenAuthHandler("secret")
        with pytest.raises(flight.FlightUnauthenticatedError):
            handler.is_valid(b"wrong")


class TestCreateAuthHandler:
    def test_default_noop(self, monkeypatch):
        monkeypatch.delenv("FLIGHT_AUTH_MODE", raising=False)
        handler = create_auth_handler()
        assert isinstance(handler, NoopAuthHandler)

    def test_token_mode(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_AUTH_MODE", "token")
        monkeypatch.setenv("FLIGHT_API_TOKEN", "my-token")
        handler = create_auth_handler()
        assert isinstance(handler, TokenAuthHandler)

    def test_token_mode_no_token_raises(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_AUTH_MODE", "token")
        monkeypatch.delenv("FLIGHT_API_TOKEN", raising=False)
        with pytest.raises(ValueError, match="FLIGHT_API_TOKEN"):
            create_auth_handler()
