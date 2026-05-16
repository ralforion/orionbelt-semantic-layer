"""Integration tests for the pgwire surface — Step 1.

Drives a live ``PgWireServer`` on an ephemeral port using raw asyncio
sockets. This avoids a hard dependency on psycopg/JDBC for the
hello-world cycle; client-library tests join in Step 4 once the
extended-query protocol exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

import pytest

from orionbelt.pgwire import protocol
from orionbelt.pgwire.server import PgWireServer


def _startup_payload(params: dict[str, str]) -> bytes:
    body = struct.pack("!I", protocol.PROTOCOL_VERSION_3)
    for key, value in params.items():
        body += key.encode() + b"\x00" + value.encode() + b"\x00"
    body += b"\x00"
    return struct.pack("!I", 4 + len(body)) + body


def _query_frame(sql: str) -> bytes:
    payload = sql.encode() + b"\x00"
    return b"Q" + struct.pack("!I", 4 + len(payload)) + payload


def _terminate_frame() -> bytes:
    return b"X" + struct.pack("!I", 4)


async def _drain_until_ready(reader: asyncio.StreamReader) -> list[tuple[bytes, bytes]]:
    """Read frames until a ReadyForQuery (``Z``) arrives."""

    frames: list[tuple[bytes, bytes]] = []
    while True:
        tag = await reader.readexactly(1)
        (length,) = struct.unpack("!I", await reader.readexactly(4))
        body = await reader.readexactly(length - 4) if length > 4 else b""
        frames.append((tag, body))
        if tag == b"Z":
            return frames


@pytest.fixture
async def pgwire_server() -> PgWireServer:
    server = PgWireServer(host="127.0.0.1", port=0, max_connections=8)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        yield server
    finally:
        await server.stop()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve_task


async def test_select_one_round_trip(pgwire_server: PgWireServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_server.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "__default__"}))
        await writer.drain()
        handshake = await _drain_until_ready(reader)
        tags = [tag for tag, _ in handshake]
        assert tags[0] == b"R"  # AuthenticationOk
        assert b"S" in tags  # at least one ParameterStatus
        assert b"K" in tags  # BackendKeyData
        assert tags[-1] == b"Z"  # ReadyForQuery

        writer.write(_query_frame("SELECT 1"))
        await writer.drain()
        reply = await _drain_until_ready(reader)
        reply_tags = [tag for tag, _ in reply]
        assert reply_tags == [b"T", b"D", b"C", b"Z"]

        # Row description should describe a single int4 column.
        _, row_desc = reply[0]
        (n_cols,) = struct.unpack("!H", row_desc[:2])
        assert n_cols == 1

        # Data row carries the literal text "1".
        _, data_row = reply[1]
        (n_vals,) = struct.unpack("!H", data_row[:2])
        assert n_vals == 1
        (col_len,) = struct.unpack("!I", data_row[2:6])
        assert col_len == 1
        assert data_row[6:7] == b"1"

        # CommandComplete carries the SELECT tag.
        _, cmd_complete = reply[2]
        assert cmd_complete.startswith(b"SELECT 1")
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_unknown_query_returns_error_response(pgwire_server: PgWireServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_server.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "__default__"}))
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(_query_frame("SELECT * FROM nope"))
        await writer.drain()
        reply = await _drain_until_ready(reader)
        reply_tags = [tag for tag, _ in reply]
        assert reply_tags == [b"E", b"Z"]
        _, err_body = reply[0]
        assert b"M" in err_body  # message field
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_ssl_request_is_rejected_then_startup_continues(
    pgwire_server: PgWireServer,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_server.bound_port)
    try:
        # SSLRequest: length=8, code=80877103.
        writer.write(struct.pack("!II", 8, protocol.SSL_REQUEST_CODE))
        await writer.drain()
        reject = await reader.readexactly(1)
        assert reject == b"N"

        writer.write(_startup_payload({"user": "obsl"}))
        await writer.drain()
        handshake = await _drain_until_ready(reader)
        assert handshake[0][0] == b"R"
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
