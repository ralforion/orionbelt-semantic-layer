"""OBSL's one sign-in that gives a new BI token its warehouse sign-in (``auth/consent.py``)."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from orionbelt.auth import OidcClient, OidcConfig, init_auth, reset_auth
from orionbelt.auth import consent as consent_module
from orionbelt.auth.consent import ConsentError, Consents
from orionbelt.auth.jwt_verify import JwtVerifier
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

SIGN_IN_AT = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_AT = f"{ISSUER}/protocol/openid-connect/token"
REDIRECT = "https://obsl.example.com/tokens/consent"


class IdentityProvider:
    """Answers OBSL's code redemption as the identity provider's token endpoint would."""

    def __init__(self, issuer: FakeIssuer) -> None:
        self.issuer = issuer
        self.requests: list[tuple[str, dict[str, str], Any]] = []
        self.signs_in = "alice"
        self.answer: dict[str, Any] | None = None
        self.status = 200

    def post(self, url: str, data: dict[str, str], auth: Any, timeout: float) -> Any:
        self.requests.append((url, data, auth))
        body = self.answer or {
            "access_token": self.issuer.token(self.signs_in),
            "refresh_token": f"rt-{self.signs_in}",
            "expires_in": 300,
        }
        return SimpleNamespace(status_code=self.status, json=lambda: body)


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[IdentityProvider]:
    issuer = FakeIssuer()
    config = OidcConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        authorization_endpoint=SIGN_IN_AT,
        token_endpoint=TOKEN_AT,
        client=OidcClient(client_id="obsl", client_secret="s3cret/+"),
        consent_scopes="openid offline_access",
    )
    init_auth(
        auth_mode="oidc", oidc=config, verifier=JwtVerifier(config, fetch=CountingFetch(issuer))
    )
    provider = IdentityProvider(issuer)
    monkeypatch.setattr(consent_module.httpx, "post", provider.post)
    yield provider
    reset_auth()


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _signed_in(consents: Consents, sub: str = "alice") -> tuple[str, str]:
    """Start a sign-in and have the identity provider send it back: (id, state)."""
    consent_id, url = consents.start(sub, REDIRECT)
    state = _query(url)["state"]
    assert consents.finish(state, "the-code") == consent_id
    return consent_id, state


class TestStart:
    def test_the_user_signs_in_as_obsl_s_client_with_pkce(self, idp: IdentityProvider) -> None:
        consent_id, url = Consents().start("alice", REDIRECT)
        assert url.startswith(SIGN_IN_AT + "?")
        query = _query(url)
        assert query["client_id"] == "obsl"
        assert query["redirect_uri"] == REDIRECT
        assert query["scope"] == "openid offline_access"
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert query["audience"] == AUDIENCE
        assert len(query["state"]) >= 32 and consent_id not in url

    def test_without_obsl_s_client_there_is_none(self, idp: IdentityProvider) -> None:
        init_auth(
            auth_mode="oidc",
            oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE, authorization_endpoint=SIGN_IN_AT),
            verifier=JwtVerifier(
                OidcConfig(issuer=ISSUER, audience=AUDIENCE), fetch=CountingFetch(idp.issuer)
            ),
        )
        with pytest.raises(ConsentError) as raised:
            Consents().start("alice", REDIRECT)
        assert raised.value.code == "WAREHOUSE_SIGN_IN_OFF"


class TestFinish:
    def test_the_code_is_redeemed_as_obsl_s_client_with_the_verifier(
        self, idp: IdentityProvider
    ) -> None:
        consents = Consents()
        consent_id, url = consents.start("alice", REDIRECT)
        consents.finish(_query(url)["state"], "the-code")
        (posted_to, form, auth), *_ = idp.requests
        assert posted_to == TOKEN_AT
        assert auth == ("obsl", "s3cret%2F%2B")  # RFC 6749 2.3.1: form-encoded first
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "the-code"
        assert form["redirect_uri"] == REDIRECT
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        assert (
            base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == _query(url)["code_challenge"]
        )
        assert consents.status(consent_id, "alice") == ("finished", "")

    def test_its_refresh_token_is_handed_over_once_to_whoever_started(
        self, idp: IdentityProvider
    ) -> None:
        consents = Consents()
        consent_id, _ = _signed_in(consents)
        with pytest.raises(ConsentError, match="unknown or has expired"):
            consents.take(consent_id, "bob")
        assert consents.take(consent_id, "alice") == "rt-alice"
        with pytest.raises(ConsentError):
            consents.take(consent_id, "alice")

    def test_another_user_signing_in_is_refused(self, idp: IdentityProvider) -> None:
        consents = Consents()
        consent_id, url = consents.start("alice", REDIRECT)
        idp.signs_in = "mallory"
        with pytest.raises(ConsentError) as raised:
            consents.finish(_query(url)["state"], "the-code")
        assert raised.value.code == "CONSENT_WRONG_USER"
        status, message = consents.status(consent_id, "alice")
        assert status == "failed" and "another user" in message
        with pytest.raises(ConsentError):
            consents.take(consent_id, "alice")

    def test_no_refresh_token_says_what_to_allow(self, idp: IdentityProvider) -> None:
        idp.answer = {"access_token": idp.issuer.token("alice"), "expires_in": 300}
        consents = Consents()
        _, url = consents.start("alice", REDIRECT)
        with pytest.raises(ConsentError, match="offline_access") as raised:
            consents.finish(_query(url)["state"], "the-code")
        assert raised.value.code == "CONSENT_NO_REFRESH_TOKEN"

    def test_a_token_not_for_obsl_s_audience_is_refused(self, idp: IdentityProvider) -> None:
        idp.answer = {
            "access_token": idp.issuer.token("alice", aud="someone-else"),
            "refresh_token": "rt",
        }
        consents = Consents()
        _, url = consents.start("alice", REDIRECT)
        with pytest.raises(ConsentError, match="OIDC_AUDIENCE") as raised:
            consents.finish(_query(url)["state"], "the-code")
        assert raised.value.code == "CONSENT_TOKEN_INVALID"

    def test_a_refused_code_is_the_identity_provider_s_error(self, idp: IdentityProvider) -> None:
        idp.status = 400
        idp.answer = {"error": "invalid_grant", "error_description": "Code not valid"}
        consents = Consents()
        _, url = consents.start("alice", REDIRECT)
        with pytest.raises(ConsentError, match="invalid_grant: Code not valid") as raised:
            consents.finish(_query(url)["state"], "the-code")
        assert raised.value.code == "CONSENT_REFUSED"

    def test_a_state_is_used_once_and_an_unknown_one_is_refused(
        self, idp: IdentityProvider
    ) -> None:
        consents = Consents()
        _, state = _signed_in(consents)
        for unknown in (state, "made-up"):
            with pytest.raises(ConsentError) as raised:
                consents.finish(unknown, "the-code")
            assert raised.value.code == "CONSENT_NOT_FOUND"
        assert len(idp.requests) == 1

    def test_the_user_cancelling_at_the_identity_provider_fails_it(
        self, idp: IdentityProvider
    ) -> None:
        consents = Consents()
        consent_id, url = consents.start("alice", REDIRECT)
        consents.fail(_query(url)["state"], "Sign-in failed: access_denied")
        assert consents.status(consent_id, "alice") == ("failed", "Sign-in failed: access_denied")
        assert idp.requests == []


class TestLifetime:
    def test_a_sign_in_lasts_ten_minutes(
        self, idp: IdentityProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        consents = Consents()
        consent_id, _ = _signed_in(consents)
        later = consent_module.time.monotonic() + consent_module.LIFETIME_SECONDS + 1
        monkeypatch.setattr(consent_module.time, "monotonic", lambda: later)
        with pytest.raises(ConsentError):
            consents.status(consent_id, "alice")
        with pytest.raises(ConsentError):
            consents.take(consent_id, "alice")

    def test_only_its_owner_sees_its_status(self, idp: IdentityProvider) -> None:
        consents = Consents()
        consent_id, _ = consents.start("alice", REDIRECT)
        assert consents.status(consent_id, "alice") == ("pending", "")
        with pytest.raises(ConsentError):
            consents.status(consent_id, "bob")

    def test_the_oldest_is_dropped_past_the_limit(
        self, idp: IdentityProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(consent_module, "_MAX_UNDER_WAY", 2)
        consents = Consents()
        first, _ = consents.start("alice", REDIRECT)
        consents.start("alice", REDIRECT)
        consents.start("alice", REDIRECT)
        with pytest.raises(ConsentError):
            consents.status(first, "alice")
