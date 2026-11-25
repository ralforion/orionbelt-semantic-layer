"""The signing key of the Dremio stack's OrionBelt with sign-in (``obsl-auth``).

``obsl-auth`` runs under ``AUTH_MODE=oidc`` and reads its identity provider's
keys from ``OIDC_JWKS_FILE``, once, at startup: the key must exist before the
stack starts, so ``run.sh`` runs this module first. The public key set is
mounted into ``obsl-auth``; the private key stays on the host (gitignored),
where the tests sign a user's access token with it, as an identity provider
would.

    python -m tests.integration.dremio.sign_in   # make the key, unless it exists
"""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from tests.oidc_tokens import FakeIssuer

KEY_DIR = Path(__file__).resolve().parent / ".auth"
#: Mounted into ``obsl-auth`` as ``/auth/jwks.json``.
JWKS_FILE = KEY_DIR / "public" / "jwks.json"
_KEY_FILE = KEY_DIR / "signing_key.pem"
_KID = "rsa-1"  # FakeIssuer's default key


def ensure_key() -> None:
    """Make the signing key and its public key set, unless both exist."""
    if _KEY_FILE.exists() and JWKS_FILE.exists():
        return
    issuer = FakeIssuer()
    key, _alg = issuer.keys[_KID]
    JWKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    _KEY_FILE.chmod(0o600)
    JWKS_FILE.write_text(json.dumps(issuer.jwks()))
    JWKS_FILE.chmod(0o644)  # read by the container's user


def issuer() -> FakeIssuer | None:
    """The issuer ``obsl-auth`` trusts, or None before ``run.sh`` made its key."""
    if not _KEY_FILE.exists():
        return None
    trusted = FakeIssuer()
    key = serialization.load_pem_private_key(_KEY_FILE.read_bytes(), password=None)
    trusted.keys[_KID] = (key, "RS256")
    return trusted


if __name__ == "__main__":
    ensure_key()
    print(f"Signing key for obsl-auth: {JWKS_FILE}")
