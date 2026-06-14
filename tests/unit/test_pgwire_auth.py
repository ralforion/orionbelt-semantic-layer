"""Unit tests for pgwire/auth.py against the shared auth subsystem."""

from __future__ import annotations

import pytest

from orionbelt.auth import init_auth, reset_auth
from orionbelt.pgwire.auth import (
    MECH_CLEARTEXT,
    MECH_SCRAM,
    authenticate,
    password_required,
    select_mechanism,
)
from orionbelt.pgwire.protocol import StartupMessage

API_KEY = "obsl_pat_unit_test_key_0123456789ab"
STARTUP = StartupMessage(protocol_version=196608, parameters={"user": "obsl"})


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_auth()
    yield
    reset_auth()


class TestNoneMode:
    def test_password_not_required(self) -> None:
        init_auth(auth_mode="none")
        assert password_required() is False

    def test_authenticate_ok_without_password(self) -> None:
        init_auth(auth_mode="none")
        assert authenticate(startup=STARTUP).ok is True


class TestApiKeyMode:
    def test_password_required(self) -> None:
        init_auth(auth_mode="api_key", api_keys=API_KEY)
        assert password_required() is True

    def test_valid_password(self) -> None:
        init_auth(auth_mode="api_key", api_keys=API_KEY)
        assert authenticate(startup=STARTUP, password=API_KEY).ok is True

    def test_wrong_password(self) -> None:
        init_auth(auth_mode="api_key", api_keys=API_KEY)
        result = authenticate(startup=STARTUP, password="nope-wrong-key-123456")
        assert result.ok is False
        assert "invalid" in result.error_message.lower()

    def test_missing_password(self) -> None:
        init_auth(auth_mode="api_key", api_keys=API_KEY)
        result = authenticate(startup=STARTUP, password=None)
        assert result.ok is False


class TestSelectMechanism:
    def test_default_is_scram(self) -> None:
        # The legacy default "trust" still selects SCRAM when auth is required.
        assert select_mechanism("trust") == MECH_SCRAM

    def test_scram_explicit(self) -> None:
        assert select_mechanism("scram-sha-256") == MECH_SCRAM

    def test_password_selects_cleartext(self) -> None:
        assert select_mechanism("password") == MECH_CLEARTEXT
        assert select_mechanism("cleartext") == MECH_CLEARTEXT
