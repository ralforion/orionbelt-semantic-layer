"""OIDC access-token verification (auth/jwt_verify.py)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from orionbelt.auth import (
    SCOPE_READ,
    SCOPE_WRITE,
    AuthConfigError,
    OidcConfig,
    TokenInvalidError,
    jwt_verify,
)
from orionbelt.auth.jwt_verify import JwtVerifier, looks_like_jwt
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _config(**overrides: object) -> OidcConfig:
    values: dict[str, object] = {"issuer": ISSUER, "audience": AUDIENCE}
    values.update(overrides)
    return OidcConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def idp() -> FakeIssuer:
    return FakeIssuer()


@pytest.fixture
def fetch(idp: FakeIssuer) -> CountingFetch:
    return CountingFetch(idp)


@pytest.fixture
def verifier(fetch: CountingFetch) -> JwtVerifier:
    v = JwtVerifier(_config(), fetch=fetch)
    v.load()
    return v


class _Clock:
    """Stands in for time.monotonic in jwt_verify, so refetch timing is exact."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(jwt_verify.time, "monotonic", c)
    return c


class TestAcceptedTokens:
    def test_user_principal_from_claims(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        principal = verifier.verify(
            idp.token("alice", scope="openid obsl:write", email="alice@example.com")
        )
        assert principal.kind == "user"
        assert principal.sub == "alice"
        assert principal.email == "alice@example.com"
        assert principal.scopes == {SCOPE_READ, SCOPE_WRITE}

    @pytest.mark.parametrize("alg", ["PS256", "ES256", "EdDSA"])
    def test_other_asymmetric_algorithms(
        self, idp: FakeIssuer, fetch: CountingFetch, alg: str
    ) -> None:
        idp.add_key(f"k-{alg}", alg)
        v = JwtVerifier(_config(), fetch=fetch)
        v.load()
        assert v.verify(idp.token(kid=f"k-{alg}")).sub == "alice"

    def test_clock_skew_is_tolerated(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        assert verifier.verify(idp.token(expires_in=-30)).sub == "alice"  # skew is 60s


class TestRefusedTokens:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"expires_in": -120}, "Token has expired"),
            ({"iss": "https://other.test"}, "Token issuer does not match OIDC_ISSUER"),
            ({"aud": "some-other-client"}, "Token audience does not include OIDC_AUDIENCE"),
        ],
    )
    def test_claims(
        self, idp: FakeIssuer, verifier: JwtVerifier, kwargs: dict[str, object], message: str
    ) -> None:
        with pytest.raises(TokenInvalidError, match=message):
            verifier.verify(idp.token(**kwargs))  # type: ignore[arg-type]

    def test_not_valid_yet(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        # nbf is taken when the test runs, not at collection: a parametrize value
        # would age while the suite runs and fall inside the 60s leeway.
        with pytest.raises(TokenInvalidError, match="Token is not valid yet"):
            verifier.verify(idp.token(nbf=int(time.time()) + 600))

    def test_no_subject(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        with pytest.raises(TokenInvalidError, match="Token has no sub claim"):
            verifier.verify(idp.token(sub=None))

    def test_signed_by_someone_else_under_the_same_key_id(self, verifier: JwtVerifier) -> None:
        forger = FakeIssuer()  # its own rsa-1, not the published one
        with pytest.raises(TokenInvalidError, match="signature does not verify"):
            verifier.verify(forger.token())

    def test_alg_none(self, verifier: JwtVerifier) -> None:
        unsigned = jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": int(time.time()) + 60},
            key=None,
            algorithm="none",
            headers={"kid": "rsa-1"},
        )
        with pytest.raises(TokenInvalidError, match="algorithm is not accepted"):
            verifier.verify(unsigned)

    def test_hs256_signed_with_the_public_key(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        # The classic confusion attack: HMAC keyed with the published public key.
        public_pem = (
            idp.keys["rsa-1"][0]
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        header = {"alg": "HS256", "kid": "rsa-1", "typ": "JWT"}
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "mallory", "exp": int(time.time()) + 60}
        # PyJWT refuses to sign this itself, so the signature is computed by hand.
        signing_input = b".".join(_b64url(json.dumps(part).encode()) for part in (header, claims))
        signature = _b64url(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
        forged = (signing_input + b"." + signature).decode()
        with pytest.raises(TokenInvalidError, match="algorithm is not accepted"):
            verifier.verify(forged)

    def test_alg_outside_the_allow_list(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        with pytest.raises(TokenInvalidError, match="algorithm is not accepted"):
            verifier.verify(idp.token(alg="RS384"))

    def test_alg_other_than_the_key_declares(self, idp: FakeIssuer, verifier: JwtVerifier) -> None:
        # The published key says RS256; a PS256 signature with it is refused.
        with pytest.raises(TokenInvalidError, match="does not match its signing key"):
            verifier.verify(idp.token(alg="PS256"))

    @pytest.mark.parametrize("garbage", ["a.b.c", "not-a-jwt", ""])
    def test_malformed(self, verifier: JwtVerifier, garbage: str) -> None:
        with pytest.raises(TokenInvalidError):
            verifier.verify(garbage)


class TestKeyRotation:
    def test_unknown_key_id_refetches_once(
        self, idp: FakeIssuer, fetch: CountingFetch, clock: _Clock
    ) -> None:
        v = JwtVerifier(_config(), fetch=fetch)
        v.load()
        fetched = len(fetch.calls)  # discovery + keys
        idp.add_key("rsa-2", "RS256")
        clock.now += 61
        assert v.verify(idp.token(kid="rsa-2")).sub == "alice"
        assert len(fetch.calls) == fetched + 1

    def test_unknown_key_ids_cannot_force_a_fetch_per_request(
        self, idp: FakeIssuer, fetch: CountingFetch, clock: _Clock
    ) -> None:
        v = JwtVerifier(_config(), fetch=fetch)
        v.load()
        fetched = len(fetch.calls)
        stranger = FakeIssuer()
        stranger.add_key("random-kid", "RS256")
        for _ in range(5):
            with pytest.raises(TokenInvalidError, match="key the issuer does not publish"):
                v.verify(stranger.token(kid="random-kid"))
        assert len(fetch.calls) == fetched  # within the 60s floor of the startup load
        clock.now += 61
        with pytest.raises(TokenInvalidError):
            v.verify(stranger.token(kid="random-kid"))
        assert len(fetch.calls) == fetched + 1

    def test_keys_are_refetched_after_the_cache_ttl(
        self, idp: FakeIssuer, fetch: CountingFetch, clock: _Clock
    ) -> None:
        v = JwtVerifier(_config(jwks_cache_ttl=3600), fetch=fetch)
        v.load()
        fetched = len(fetch.calls)
        clock.now += 3601
        v.verify(idp.token())
        assert len(fetch.calls) == fetched + 1

    def test_a_failed_refetch_keeps_the_old_keys(
        self, idp: FakeIssuer, fetch: CountingFetch, clock: _Clock
    ) -> None:
        v = JwtVerifier(_config(jwks_cache_ttl=3600), fetch=fetch)
        v.load()
        fetch.fail = True
        clock.now += 3601
        assert v.verify(idp.token()).sub == "alice"


class TestLoading:
    def test_discovery_then_keys(self, fetch: CountingFetch) -> None:
        JwtVerifier(_config(), fetch=fetch).load()
        assert fetch.calls == [
            f"{ISSUER}/.well-known/openid-configuration",
            f"{ISSUER}/protocol/openid-connect/certs",
        ]

    def test_jwks_uri_skips_discovery(self, fetch: CountingFetch) -> None:
        JwtVerifier(_config(jwks_uri="https://keys.test/jwks"), fetch=fetch).load()
        assert fetch.calls == ["https://keys.test/jwks"]

    def test_jwks_file_is_never_fetched(
        self, idp: FakeIssuer, fetch: CountingFetch, tmp_path: Path
    ) -> None:
        path = tmp_path / "jwks.json"
        path.write_text(json.dumps(idp.jwks()))
        v = JwtVerifier(_config(jwks_file=str(path)), fetch=fetch)
        v.load()
        idp.add_key("rsa-2", "RS256")
        with pytest.raises(TokenInvalidError):
            v.verify(idp.token(kid="rsa-2"))
        assert v.verify(idp.token()).sub == "alice"
        assert fetch.calls == []

    def test_issuer_mismatch_in_discovery_refuses_to_start(self, idp: FakeIssuer) -> None:
        fetch = CountingFetch(idp)
        idp.issuer = ISSUER + "/"  # what the document says differs by a trailing slash
        with pytest.raises(AuthConfigError, match="must match exactly"):
            JwtVerifier(_config(), fetch=fetch).load()

    def test_unreachable_idp_refuses_to_start(self, fetch: CountingFetch) -> None:
        fetch.fail = True
        with pytest.raises(AuthConfigError, match="could not load the signing keys"):
            JwtVerifier(_config(), fetch=fetch).load()

    def test_encryption_keys_are_not_signing_keys(self, idp: FakeIssuer, tmp_path: Path) -> None:
        jwks = idp.jwks()
        jwks["keys"][0]["use"] = "enc"
        path = tmp_path / "jwks.json"
        path.write_text(json.dumps(jwks))
        with pytest.raises(AuthConfigError, match="no usable signing key"):
            JwtVerifier(_config(jwks_file=str(path))).load()


def test_looks_like_jwt(idp: FakeIssuer) -> None:
    assert looks_like_jwt(idp.token())
    assert not looks_like_jwt("obsl_pat_0123456789abcdef0123456789abcdef01234567")
    assert not looks_like_jwt("a.b.c.d.e")  # JWE: not accepted
