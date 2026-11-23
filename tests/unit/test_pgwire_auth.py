"""Unit tests for pgwire/auth.py against the shared auth subsystem."""

from __future__ import annotations

import pytest

from orionbelt.auth import OidcConfig, init_auth, reset_auth
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.pgwire.auth import (
    MECH_CLEARTEXT,
    MECH_SCRAM,
    authenticate,
    password_required,
    select_mechanism,
)
from orionbelt.pgwire.protocol import StartupMessage
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

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


class TestOidcMode:
    """Under AUTH_MODE=oidc pgwire still requires a credential: a service key."""

    @pytest.fixture(autouse=True)
    def _oidc(self) -> None:
        idp = FakeIssuer()
        config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)
        init_auth(
            auth_mode="oidc",
            api_keys=API_KEY,
            oidc=config,
            verifier=JwtVerifier(config, fetch=CountingFetch(idp)),
        )
        self.idp = idp
        self.token = idp.token()

    def test_password_required(self) -> None:
        # Not trust: before this change any non-api_key mode fell through to it.
        assert password_required() is True

    def test_service_key(self) -> None:
        assert authenticate(startup=STARTUP, password=API_KEY).ok is True

    def test_service_key_is_the_api_key_caller(self) -> None:
        result = authenticate(startup=STARTUP, password=API_KEY)
        assert result.principal is not None and result.principal.kind == "api_key"

    def test_a_token_over_tls_is_its_user(self) -> None:
        result = authenticate(startup=STARTUP, password=self.token, tls=True)
        assert result.ok is True
        assert result.principal is not None and result.principal.sub == "alice"

    def test_a_token_without_tls_is_refused_and_says_why(self) -> None:
        result = authenticate(startup=STARTUP, password=self.token, tls=False)
        assert result.ok is False
        assert "accepted only over TLS" in result.error_message

    def test_a_bad_token_says_why(self) -> None:
        result = authenticate(startup=STARTUP, password=self.idp.token(expires_in=-300), tls=True)
        assert (result.ok, result.error_message) == (False, "Token has expired")

    def test_a_token_needs_obsl_read(self) -> None:
        result = authenticate(startup=STARTUP, password=self.idp.token(scope="openid"), tls=True)
        assert (result.ok, result.error_message) == (
            False,
            "insufficient scope: requires obsl:read",
        )

    def test_tls_gets_cleartext_so_a_token_can_be_sent(self) -> None:
        assert select_mechanism("trust", tls=True) == MECH_CLEARTEXT
        assert select_mechanism("trust", tls=False) == MECH_SCRAM


class TestSelectMechanismUnderApiKey:
    def test_tls_does_not_change_the_mechanism(self) -> None:
        init_auth(auth_mode="api_key", api_keys=API_KEY)
        assert select_mechanism("trust", tls=True) == MECH_SCRAM


class TestSelectMechanism:
    def test_default_is_scram(self) -> None:
        # The legacy default "trust" still selects SCRAM when auth is required.
        assert select_mechanism("trust") == MECH_SCRAM

    def test_scram_explicit(self) -> None:
        assert select_mechanism("scram-sha-256") == MECH_SCRAM

    def test_password_selects_cleartext(self) -> None:
        assert select_mechanism("password") == MECH_CLEARTEXT
        assert select_mechanism("cleartext") == MECH_CLEARTEXT
