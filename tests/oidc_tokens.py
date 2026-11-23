"""A stand-in OIDC issuer for tests: key pairs, their JWK Set, and minted tokens."""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

ISSUER = "https://idp.test/realms/obsl"
AUDIENCE = "obsl-api"


class FakeIssuer:
    """Signs tokens the way an IdP would and publishes the matching keys."""

    def __init__(self, issuer: str = ISSUER, audience: str = AUDIENCE) -> None:
        self.issuer = issuer
        self.audience = audience
        self.keys: dict[str, tuple[Any, str]] = {}  # kid -> (private key, alg)
        self.add_key("rsa-1", "RS256")

    def add_key(self, kid: str, alg: str) -> None:
        """Create a key pair for ``alg`` under ``kid`` (rotation: add, then sign with it)."""
        if alg in ("RS256", "PS256"):
            key: Any = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        elif alg == "ES256":
            key = ec.generate_private_key(ec.SECP256R1())
        elif alg == "EdDSA":
            key = ed25519.Ed25519PrivateKey.generate()
        else:
            raise ValueError(alg)
        self.keys[kid] = (key, alg)

    def jwks(self, *, declare_alg: bool = True) -> dict[str, Any]:
        """The public JWK Set, as the IdP's ``jwks_uri`` serves it."""
        out = []
        for kid, (key, alg) in self.keys.items():
            if alg in ("RS256", "PS256"):
                jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            elif alg == "ES256":
                jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
            else:
                jwk = OKPAlgorithm.to_jwk(key.public_key(), as_dict=True)
            jwk.update({"kid": kid, "use": "sig"})
            if declare_alg:
                jwk["alg"] = alg
            out.append(jwk)
        return {"keys": out}

    def discovery(self) -> dict[str, Any]:
        return {"issuer": self.issuer, "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs"}

    def token(
        self,
        sub: str | None = "alice",
        *,
        scope: str | None = "obsl:read",
        kid: str = "rsa-1",
        alg: str | None = None,
        expires_in: int = 300,
        **claims: Any,
    ) -> str:
        """Mint an access token; ``claims`` override or add to the defaults."""
        key, key_alg = self.keys[kid]
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": now + expires_in,
        }
        if sub is not None:
            payload["sub"] = sub
        if scope is not None:
            payload["scope"] = scope
        payload.update(claims)
        return jwt.encode(payload, key, algorithm=alg or key_alg, headers={"kid": kid})


class CountingFetch:
    """A fetch for :class:`JwtVerifier` that serves the issuer's documents and counts."""

    def __init__(self, issuer: FakeIssuer) -> None:
        self.issuer = issuer
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise OSError("IdP unreachable")
        if url.endswith("/.well-known/openid-configuration"):
            return self.issuer.discovery()
        return self.issuer.jwks()
