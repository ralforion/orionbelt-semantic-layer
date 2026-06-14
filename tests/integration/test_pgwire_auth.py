"""pgwire auth integration tests (design/PLAN_authentication.md Phase 2 + SCRAM).

Drives a live PgWireServer on an ephemeral port and exercises both password
mechanisms that AUTH_MODE=api_key can trigger: cleartext (opt-in) and the
default SCRAM-SHA-256. The SCRAM client side is implemented independently in
this file so the round trip validates the server's real crypto.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import secrets
import struct

import pytest

from orionbelt.auth import init_auth, reset_auth
from orionbelt.pgwire import protocol
from orionbelt.pgwire.server import PgWireServer

API_KEY = "obsl_pat_pgwire_test_key_0123456789"


# --- frame helpers ---


def _startup_payload(params: dict[str, str]) -> bytes:
    body = struct.pack("!I", protocol.PROTOCOL_VERSION_3)
    for key, value in params.items():
        body += key.encode() + b"\x00" + value.encode() + b"\x00"
    body += b"\x00"
    return struct.pack("!I", 4 + len(body)) + body


def _password_frame(password: str) -> bytes:
    payload = password.encode() + b"\x00"
    return b"p" + struct.pack("!I", 4 + len(payload)) + payload


def _sasl_initial_frame(mechanism: str, client_first: str) -> bytes:
    cf = client_first.encode()
    payload = mechanism.encode() + b"\x00" + struct.pack("!i", len(cf)) + cf
    return b"p" + struct.pack("!I", 4 + len(payload)) + payload


def _sasl_response_frame(client_final: str) -> bytes:
    payload = client_final.encode()
    return b"p" + struct.pack("!I", 4 + len(payload)) + payload


def _terminate_frame() -> bytes:
    return b"X" + struct.pack("!I", 4)


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    tag = await reader.readexactly(1)
    (length,) = struct.unpack("!I", await reader.readexactly(4))
    body = await reader.readexactly(length - 4) if length > 4 else b""
    return tag, body


async def _drain_until(reader: asyncio.StreamReader, stop: bytes) -> list[tuple[bytes, bytes]]:
    frames: list[tuple[bytes, bytes]] = []
    while True:
        tag, body = await _read_frame(reader)
        frames.append((tag, body))
        if tag == stop:
            return frames


# --- minimal SCRAM-SHA-256 client (independent of the server impl) ---


def _scram_attrs(message: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in message.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def _scram_client_final(password: str, bare: str, server_first: str) -> str:
    attrs = _scram_attrs(server_first)
    combined_nonce = attrs["r"]
    salt = base64.b64decode(attrs["s"])
    iterations = int(attrs["i"])
    channel = base64.b64encode(b"n,,").decode("ascii")  # "biws"
    without_proof = f"c={channel},r={combined_nonce}"

    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    auth_message = f"{bare},{server_first},{without_proof}".encode()
    client_sig = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, client_sig, strict=True))
    return f"{without_proof},p={base64.b64encode(proof).decode('ascii')}"


# --- fixtures ---


@pytest.fixture(autouse=True)
def _reset_auth_after():
    yield
    reset_auth()


async def _make_server(auth_mode: str = "trust") -> PgWireServer:
    server = PgWireServer(host="127.0.0.1", port=0, auth_mode=auth_mode, max_connections=8)
    await server.start()
    return server


@contextlib.asynccontextmanager
async def _running(server: PgWireServer):
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        yield server
    finally:
        await server.stop()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve_task


# --- trust mode (default, AUTH_MODE=none) ---


async def test_trust_mode_no_password() -> None:
    server = await _make_server()
    async with _running(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            writer.write(_startup_payload({"user": "obsl", "database": "__default__"}))
            await writer.drain()
            handshake = await _drain_until(reader, b"Z")
            assert handshake[0][0] == b"R"
            assert struct.unpack("!I", handshake[0][1][:4])[0] == 0  # AuthenticationOk
        finally:
            writer.write(_terminate_frame())
            await writer.drain()
            writer.close()
            await writer.wait_closed()


# --- cleartext mode (PGWIRE_AUTH_MODE=password) ---


async def test_cleartext_valid_password() -> None:
    init_auth(auth_mode="api_key", api_keys=API_KEY)
    server = await _make_server(auth_mode="password")
    async with _running(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            writer.write(_startup_payload({"user": "tableau", "database": "__default__"}))
            await writer.drain()
            tag, body = await _read_frame(reader)
            assert tag == b"R" and struct.unpack("!I", body[:4])[0] == 3  # cleartext request
            writer.write(_password_frame(API_KEY))
            await writer.drain()
            handshake = await _drain_until(reader, b"Z")
            assert struct.unpack("!I", handshake[0][1][:4])[0] == 0  # AuthenticationOk
        finally:
            writer.write(_terminate_frame())
            await writer.drain()
            writer.close()
            await writer.wait_closed()


async def test_cleartext_wrong_password() -> None:
    init_auth(auth_mode="api_key", api_keys=API_KEY)
    server = await _make_server(auth_mode="password")
    async with _running(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            writer.write(_startup_payload({"user": "obsl", "database": "__default__"}))
            await writer.drain()
            await _read_frame(reader)  # cleartext request
            writer.write(_password_frame("wrong-key-totally-invalid"))
            await writer.drain()
            tag, body = await _read_frame(reader)
            assert tag == b"E"
            assert b"28P01" in body
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# --- SCRAM-SHA-256 mode (default when AUTH_MODE=api_key) ---


async def _scram_handshake(reader, writer, password: str) -> list[tuple[bytes, bytes]]:
    """Run the client side through SASLFinal; return frames read after it."""
    writer.write(_startup_payload({"user": "powerbi", "database": "__default__"}))
    await writer.drain()

    tag, body = await _read_frame(reader)
    assert tag == b"R" and struct.unpack("!I", body[:4])[0] == 10  # AuthenticationSASL

    client_nonce = base64.b64encode(secrets.token_bytes(12)).decode("ascii")
    bare = f"n=,r={client_nonce}"
    writer.write(_sasl_initial_frame("SCRAM-SHA-256", f"n,,{bare}"))
    await writer.drain()

    tag, body = await _read_frame(reader)
    assert tag == b"R" and struct.unpack("!I", body[:4])[0] == 11  # SASLContinue
    server_first = body[4:].decode("utf-8")

    client_final = _scram_client_final(password, bare, server_first)
    writer.write(_sasl_response_frame(client_final))
    await writer.drain()
    return []


async def test_scram_valid_key() -> None:
    init_auth(auth_mode="api_key", api_keys=API_KEY)
    server = await _make_server()  # default → SCRAM
    async with _running(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            await _scram_handshake(reader, writer, API_KEY)
            tag, body = await _read_frame(reader)
            assert tag == b"R" and struct.unpack("!I", body[:4])[0] == 12  # SASLFinal
            assert body[4:].startswith(b"v=")  # server signature
            rest = await _drain_until(reader, b"Z")
            assert struct.unpack("!I", rest[0][1][:4])[0] == 0  # AuthenticationOk
        finally:
            writer.write(_terminate_frame())
            await writer.drain()
            writer.close()
            await writer.wait_closed()


# --- DoS hardening: auth timeout + frame-size cap ---


async def test_auth_handshake_timeout_closes_stalled_client() -> None:
    """A client that connects but never sends a startup packet is dropped."""
    server = PgWireServer(host="127.0.0.1", port=0, auth_timeout_seconds=0.3, max_connections=8)
    await server.start()
    async with _running(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            # Send nothing. The server must give up within the auth deadline and
            # either send a FATAL error or close the socket, freeing the slot.
            tag = await asyncio.wait_for(reader.read(1), timeout=3.0)
            # Either an ErrorResponse frame ('E') or EOF (b"") is acceptable.
            assert tag in (b"E", b"")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def test_read_message_rejects_oversized_frame() -> None:
    """read_message raises before reading an over-cap body."""

    async def fake_read(n: int) -> bytes:
        if n == 1:
            return b"p"
        if n == 4:
            return struct.pack("!I", protocol.MAX_AUTH_FRAME_SIZE + 100)  # length field
        raise AssertionError("must not attempt to read the oversized body")

    with pytest.raises(protocol.ProtocolError, match="exceeds cap"):
        await protocol.read_message(fake_read, max_length=protocol.MAX_AUTH_FRAME_SIZE)


async def test_scram_wrong_key() -> None:
    init_auth(auth_mode="api_key", api_keys=API_KEY)
    server = await _make_server()
    async with _running(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            await _scram_handshake(reader, writer, "the-wrong-key-9876543210xyz")
            tag, body = await _read_frame(reader)
            assert tag == b"E"  # FATAL ErrorResponse, no SASLFinal
            assert b"28P01" in body
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
