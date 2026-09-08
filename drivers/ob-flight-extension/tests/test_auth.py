"""Tests for Flight authentication handlers."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from pyarrow import flight

from ob_flight.auth import (
    AuthMiddlewareFactory,
    NoopAuthHandler,
    SharedKeyAuthHandler,
    TokenAuthHandler,
    _credential_from_headers,
    create_auth_handler,
)


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


class TestSharedKeyAuthHandler:
    def test_authenticate_success(self):
        handler = SharedKeyAuthHandler(lambda key: key == "good-key")
        incoming = MagicMock()
        incoming.read.return_value = b"good-key"
        outgoing = MagicMock()
        handler.authenticate(outgoing, incoming)
        outgoing.write.assert_called_once_with(b"good-key")

    def test_authenticate_failure(self):
        handler = SharedKeyAuthHandler(lambda key: key == "good-key")
        incoming = MagicMock()
        incoming.read.return_value = b"bad-key"
        with pytest.raises(flight.FlightUnauthenticatedError):
            handler.authenticate(MagicMock(), incoming)

    def test_is_valid_success(self):
        handler = SharedKeyAuthHandler(lambda key: key == "good-key")
        assert handler.is_valid(b"good-key") == "authenticated"

    def test_is_valid_failure(self):
        handler = SharedKeyAuthHandler(lambda key: key == "good-key")
        with pytest.raises(flight.FlightUnauthenticatedError):
            handler.is_valid(b"bad-key")


class TestCreateAuthHandler:
    def test_default_noop(self, monkeypatch):
        monkeypatch.delenv("FLIGHT_AUTH_MODE", raising=False)
        handler = create_auth_handler()
        assert isinstance(handler, NoopAuthHandler)

    def test_validate_fn_yields_shared_handler(self, monkeypatch):
        # When a shared validator is supplied it wins over the legacy env path.
        monkeypatch.setenv("FLIGHT_AUTH_MODE", "token")
        monkeypatch.setenv("FLIGHT_API_TOKEN", "legacy")
        handler = create_auth_handler(validate_fn=lambda key: True)
        assert isinstance(handler, SharedKeyAuthHandler)

    def test_token_mode_legacy_still_works(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_AUTH_MODE", "token")
        monkeypatch.setenv("FLIGHT_API_TOKEN", "my-token")
        handler = create_auth_handler()
        assert isinstance(handler, TokenAuthHandler)

    def test_token_mode_no_token_raises(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_AUTH_MODE", "token")
        monkeypatch.delenv("FLIGHT_API_TOKEN", raising=False)
        with pytest.raises(ValueError, match="FLIGHT_API_TOKEN"):
            create_auth_handler()


class TestCredentialFromHeaders:
    """The decoder behind ``AuthenticateBasicToken``.

    ``SharedKeyAuthHandler`` above answers Flight's *legacy* ``Handshake``.
    Current clients use ``AuthenticateBasicToken`` instead, which puts the
    credential in a call header - so this is the path a real ADBC connection
    takes, and these are the shapes it arrives in.
    """

    @pytest.mark.parametrize(
        ("key", "padding"),
        [("k" * 34, 0), ("k" * 33, 1), ("k" * 35, 2)],
        ids=["no-padding", "one-pad", "two-pads"],
    )
    def test_basic_is_accepted_without_its_padding(self, key: str, padding: int) -> None:
        """ADBC sends the base64 unpadded and ``b64decode`` rejects that, so a
        correct key looked like no key at all. All three residues are covered
        because a key whose length happens to need no padding proves nothing.
        """
        raw = f"obsl:{key}".encode()
        encoded = base64.b64encode(raw).decode()
        assert encoded.count("=") == padding, "this case does not test what it claims"

        assert _credential_from_headers({"authorization": f"Basic {encoded.rstrip('=')}"}) == key

    def test_the_username_is_ignored(self):
        # OBSL has keys, not accounts - whatever the client puts before the
        # colon is not a subject we can authenticate.
        encoded = base64.b64encode(b"anyone-at-all:the-key").decode()
        assert _credential_from_headers({"authorization": f"Basic {encoded}"}) == "the-key"

    def test_basic_without_a_colon_is_not_a_credential(self):
        encoded = base64.b64encode(b"no-colon-here").decode()
        assert _credential_from_headers({"authorization": f"Basic {encoded}"}) is None

    def test_undecodable_basic_is_not_a_credential(self):
        assert _credential_from_headers({"authorization": "Basic !!!not-base64!!!"}) is None

    def test_bearer_is_taken_verbatim(self):
        assert _credential_from_headers({"authorization": "Bearer the-key"}) == "the-key"

    def test_the_scheme_is_matched_case_insensitively(self):
        # gRPC lowercases header names; clients vary on the scheme itself.
        assert _credential_from_headers({"authorization": "bearer the-key"}) == "the-key"

    def test_x_api_key_is_read(self):
        assert _credential_from_headers({"x-api-key": "the-key"}) == "the-key"

    def test_header_values_may_arrive_as_lists(self):
        # Flight hands middleware the gRPC metadata, where each name maps to
        # every value sent under it.
        assert _credential_from_headers({"x-api-key": ["the-key"]}) == "the-key"

    def test_no_credential_at_all(self):
        assert _credential_from_headers({"user-agent": ["adbc"]}) is None


class TestAuthMiddlewareFactory:
    def test_a_valid_key_passes_and_is_echoed_back(self):
        factory = AuthMiddlewareFactory(lambda key: key == "good-key")
        middleware = factory.start_call(MagicMock(), {"x-api-key": "good-key"})
        # ``AuthenticateBasicToken`` expects the token to use from here on in
        # the response headers; without this the client reports that it never
        # got one, even though the key was right.
        assert middleware.sending_headers() == {"authorization": "Bearer good-key"}

    def test_a_wrong_key_is_refused(self):
        factory = AuthMiddlewareFactory(lambda key: key == "good-key")
        with pytest.raises(flight.FlightUnauthenticatedError):
            factory.start_call(MagicMock(), {"x-api-key": "bad-key"})

    def test_a_missing_key_is_refused(self):
        factory = AuthMiddlewareFactory(lambda key: key == "good-key")
        with pytest.raises(flight.FlightUnauthenticatedError):
            factory.start_call(MagicMock(), {})
