"""SCRAM-SHA-256 server-side exchange for the pgwire surface.

Implements the server half of RFC 5802 / RFC 7677 as used by the Postgres
SASL authentication flow. Unlike a normal Postgres server we do not store a
salted verifier — OBSL holds the cleartext API keys (from ``API_KEYS``), so
the server picks a fresh random salt per handshake and, at verification time,
tries each configured key against the client proof. Any match authenticates.

SCRAM never transmits the key on the wire (only a proof of knowledge), which
is why it is the secure default over cleartext password auth on non-TLS
connections. Channel binding (SCRAM-SHA-256-PLUS) is intentionally not
advertised — it requires TLS in-process, which the pgwire surface does not
terminate. See design/PLAN_authentication.md §3.3.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Iterable

SCRAM_SHA_256 = "SCRAM-SHA-256"
DEFAULT_ITERATIONS = 4096
_GS2_NO_CBIND = "n,,"


class ScramError(Exception):
    """Raised on a malformed or non-conforming SCRAM message."""


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _sha256(msg: bytes) -> bytes:
    return hashlib.sha256(msg).digest()


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def _salted_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _parse_attributes(message: str) -> dict[str, str]:
    """Parse a comma-separated ``key=value`` SCRAM attribute string.

    Only the first ``=`` splits each attribute, so base64 values containing
    ``=`` padding survive intact.
    """
    attrs: dict[str, str] = {}
    for part in message.split(","):
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        attrs[key] = value
    return attrs


class ScramServerExchange:
    """Drives the server side of one SCRAM-SHA-256 handshake.

    Usage::

        ex = ScramServerExchange(candidate_keys)
        server_first = ex.handle_client_first(client_first_message)
        server_final = ex.handle_client_final(client_final_message)  # raises on failure
    """

    def __init__(
        self,
        candidate_keys: Iterable[str],
        *,
        iterations: int = DEFAULT_ITERATIONS,
        salt: bytes | None = None,
        server_nonce: str | None = None,
    ) -> None:
        self._keys = tuple(candidate_keys)
        self._iterations = iterations
        self._salt = salt if salt is not None else secrets.token_bytes(16)
        # Nonce must be printable ASCII without a comma; base64 satisfies that.
        self._server_nonce = server_nonce or base64.b64encode(secrets.token_bytes(18)).decode(
            "ascii"
        )
        self._client_first_bare: str | None = None
        self._server_first: str | None = None
        self._combined_nonce: str | None = None
        self._gs2_header: str = _GS2_NO_CBIND

    def handle_client_first(self, client_first_message: str) -> str:
        """Consume client-first-message, return server-first-message."""
        # client-first = gs2-header + client-first-bare. The gs2 header is the
        # cbind flag, an optional authzid, then the bare part: "<flag>,<authzid>,<bare>".
        parts = client_first_message.split(",", 2)
        if len(parts) < 3:
            raise ScramError("malformed client-first-message")
        cbind_flag, authzid, bare = parts
        if cbind_flag not in ("n", "y"):
            # 'p=' would request channel binding, which we do not advertise.
            raise ScramError(f"unsupported channel-binding flag: {cbind_flag!r}")
        self._gs2_header = f"{cbind_flag},{authzid},"
        self._client_first_bare = bare

        attrs = _parse_attributes(bare)
        client_nonce = attrs.get("r")
        if not client_nonce:
            raise ScramError("client-first-message missing nonce")

        self._combined_nonce = client_nonce + self._server_nonce
        salt_b64 = base64.b64encode(self._salt).decode("ascii")
        self._server_first = f"r={self._combined_nonce},s={salt_b64},i={self._iterations}"
        return self._server_first

    def handle_client_final(self, client_final_message: str) -> str:
        """Verify client-final-message; return server-final-message or raise.

        Tries every candidate key against the client proof. Returns the
        server-final-message (``v=<ServerSignature>``) on the first match;
        raises :class:`ScramError` if no key matches.
        """
        if self._client_first_bare is None or self._server_first is None:
            raise ScramError("client-final received before client-first")

        attrs = _parse_attributes(client_final_message)
        channel_binding = attrs.get("c")
        nonce = attrs.get("r")
        proof_b64 = attrs.get("p")
        if not channel_binding or not nonce or not proof_b64:
            raise ScramError("client-final-message missing c/r/p")

        # The channel-binding attribute must echo base64(gs2-header) for the
        # no-cbind case we advertised.
        expected_cbind = base64.b64encode(self._gs2_header.encode("ascii")).decode("ascii")
        if channel_binding != expected_cbind:
            raise ScramError("channel-binding mismatch")
        if nonce != self._combined_nonce:
            raise ScramError("nonce mismatch")

        client_final_without_proof = client_final_message.split(",p=", 1)[0]
        auth_message = (
            f"{self._client_first_bare},{self._server_first},{client_final_without_proof}"
        ).encode("ascii")

        try:
            client_proof = base64.b64decode(proof_b64)
        except Exception as exc:  # noqa: BLE001 - malformed base64 is a client error
            raise ScramError("invalid client proof encoding") from exc

        for key in self._keys:
            server_signature = self._verify_one(key, auth_message, client_proof)
            if server_signature is not None:
                return f"v={base64.b64encode(server_signature).decode('ascii')}"
        raise ScramError("no matching key")

    def _verify_one(self, key: str, auth_message: bytes, client_proof: bytes) -> bytes | None:
        """Return the ServerSignature when ``key`` matches the proof, else None."""
        salted = _salted_password(key, self._salt, self._iterations)
        client_key = _hmac(salted, b"Client Key")
        stored_key = _sha256(client_key)
        client_signature = _hmac(stored_key, auth_message)
        if len(client_proof) != len(client_signature):
            return None
        recovered_client_key = _xor(client_proof, client_signature)
        if not hmac.compare_digest(_sha256(recovered_client_key), stored_key):
            return None
        server_key = _hmac(salted, b"Server Key")
        return _hmac(server_key, auth_message)
