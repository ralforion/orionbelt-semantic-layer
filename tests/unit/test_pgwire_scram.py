"""Unit tests for the SCRAM-SHA-256 server exchange (pgwire/scram.py)."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from orionbelt.pgwire.scram import ScramError, ScramServerExchange

KEY = "obsl_pat_scram_unit_key_0123456789ab"


def _client_final(password: str, bare: str, server_first: str) -> str:
    attrs = dict(p.split("=", 1) for p in server_first.split(",") if "=" in p)
    salt = base64.b64decode(attrs["s"])
    iterations = int(attrs["i"])
    channel = base64.b64encode(b"n,,").decode("ascii")
    without_proof = f"c={channel},r={attrs['r']}"
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client_key).digest()
    auth_msg = f"{bare},{server_first},{without_proof}".encode()
    client_sig = hmac.new(stored, auth_msg, hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, client_sig, strict=True))
    return f"{without_proof},p={base64.b64encode(proof).decode('ascii')}"


def test_full_exchange_valid_key() -> None:
    ex = ScramServerExchange([KEY])
    bare = "n=,r=clientnonce123"
    server_first = ex.handle_client_first(f"n,,{bare}")
    assert server_first.startswith("r=clientnonce123")
    assert ",s=" in server_first and ",i=" in server_first

    server_final = ex.handle_client_final(_client_final(KEY, bare, server_first))
    assert server_final.startswith("v=")


def test_wrong_key_rejected() -> None:
    ex = ScramServerExchange([KEY])
    bare = "n=,r=abc"
    server_first = ex.handle_client_first(f"n,,{bare}")
    # Client computes its proof with a different password than the server holds.
    bad_final = _client_final("a-different-key-entirely-9999", bare, server_first)
    with pytest.raises(ScramError, match="no matching key"):
        ex.handle_client_final(bad_final)


def test_multiple_keys_one_matches() -> None:
    ex = ScramServerExchange(["other-key-aaaaaaaaaaaa", KEY, "third-key-bbbbbbbbbbbb"])
    bare = "n=,r=zzz"
    server_first = ex.handle_client_first(f"n,,{bare}")
    assert ex.handle_client_final(_client_final(KEY, bare, server_first)).startswith("v=")


def test_nonce_mismatch_rejected() -> None:
    ex = ScramServerExchange([KEY])
    bare = "n=,r=clientnonce"
    ex.handle_client_first(f"n,,{bare}")
    # Tamper the nonce in client-final.
    with pytest.raises(ScramError, match="nonce mismatch"):
        ex.handle_client_final("c=biws,r=tampered,p=AAAA")


def test_client_first_missing_nonce() -> None:
    ex = ScramServerExchange([KEY])
    with pytest.raises(ScramError, match="missing nonce"):
        ex.handle_client_first("n,,n=")


def test_channel_binding_request_rejected() -> None:
    ex = ScramServerExchange([KEY])
    # 'p=' cbind flag requests channel binding we never advertise.
    with pytest.raises(ScramError, match="channel-binding"):
        ex.handle_client_first("p=tls-server-end-point,,n=,r=abc")
