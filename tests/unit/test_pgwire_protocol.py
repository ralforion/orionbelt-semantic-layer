"""Unit tests for pgwire frame readers and writers."""

from __future__ import annotations

import asyncio
import struct

import pytest

from orionbelt.pgwire import protocol


def test_build_authentication_ok() -> None:
    frame = protocol.build_authentication_ok()
    assert frame[:1] == b"R"
    (length,) = struct.unpack("!I", frame[1:5])
    assert length == 8
    (auth_type,) = struct.unpack("!I", frame[5:9])
    assert auth_type == 0


def test_build_parameter_status_roundtrip() -> None:
    frame = protocol.build_parameter_status("server_encoding", "UTF8")
    assert frame[:1] == b"S"
    (length,) = struct.unpack("!I", frame[1:5])
    payload = frame[5 : 1 + length]
    assert payload == b"server_encoding\x00UTF8\x00"


def test_build_ready_for_query_idle() -> None:
    frame = protocol.build_ready_for_query()
    assert frame == b"Z" + struct.pack("!I", 5) + b"I"


def test_build_ready_for_query_rejects_multi_byte_status() -> None:
    with pytest.raises(ValueError):
        protocol.build_ready_for_query(status=b"IDLE")


def test_build_row_description_single_int4_column() -> None:
    frame = protocol.build_row_description([("?column?", protocol.OID_INT4)])
    assert frame[:1] == b"T"
    (length,) = struct.unpack("!I", frame[1:5])
    payload = frame[5 : 1 + length]
    (n_cols,) = struct.unpack("!H", payload[:2])
    assert n_cols == 1
    # Name + NUL + (table_oid I, col_attr h, type_oid I, type_size h,
    # type_mod i, format h) = 4+2+4+2+4+2 = 18 bytes after the name.
    name, rest = payload[2:].split(b"\x00", 1)
    assert name == b"?column?"
    table_oid, col_attr, type_oid, type_size, type_mod, fmt = struct.unpack("!IhIhih", rest[:18])
    assert table_oid == 0
    assert col_attr == 0
    assert type_oid == protocol.OID_INT4
    assert type_size == -1
    assert type_mod == -1
    assert fmt == 0


def test_build_data_row_text_value() -> None:
    frame = protocol.build_data_row(["1"])
    assert frame[:1] == b"D"
    (length,) = struct.unpack("!I", frame[1:5])
    payload = frame[5 : 1 + length]
    (n,) = struct.unpack("!H", payload[:2])
    assert n == 1
    (col_len,) = struct.unpack("!I", payload[2:6])
    assert col_len == 1
    assert payload[6:7] == b"1"


def test_build_data_row_null() -> None:
    frame = protocol.build_data_row([None])
    payload = frame[5:]
    (n,) = struct.unpack("!H", payload[:2])
    assert n == 1
    (col_len,) = struct.unpack("!i", payload[2:6])
    assert col_len == -1


def test_build_command_complete() -> None:
    frame = protocol.build_command_complete("SELECT 1")
    assert frame[:1] == b"C"
    assert frame.endswith(b"SELECT 1\x00")


def test_build_error_response_carries_required_fields() -> None:
    frame = protocol.build_error_response(severity="ERROR", code="0A000", message="not supported")
    assert frame[:1] == b"E"
    payload = frame[5:]
    assert b"SERROR\x00" in payload
    assert b"C0A000\x00" in payload
    assert b"Mnot supported\x00" in payload
    assert payload.endswith(b"\x00\x00")


def test_parse_query_strips_nul() -> None:
    msg = protocol.parse_query(b"SELECT 1\x00")
    assert msg.sql == "SELECT 1"


def test_parse_query_rejects_unterminated() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_query(b"SELECT 1")


# ---------------------------------------------------------------------------
# StartupMessage reader — drive it from an in-memory byte buffer.
# ---------------------------------------------------------------------------


class _ByteReader:
    """Minimal stand-in for asyncio.StreamReader.readexactly."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._offset : self._offset + n]
        if len(chunk) != n:
            raise asyncio.IncompleteReadError(chunk, n)
        self._offset += n
        return chunk


def _build_startup_payload(params: dict[str, str]) -> bytes:
    body = struct.pack("!I", protocol.PROTOCOL_VERSION_3)
    for key, value in params.items():
        body += key.encode() + b"\x00" + value.encode() + b"\x00"
    body += b"\x00"
    return struct.pack("!I", 4 + len(body)) + body


def test_read_startup_message_parses_parameters() -> None:
    payload = _build_startup_payload({"user": "obsl", "database": "__default__"})
    reader = _ByteReader(payload)
    startup = asyncio.run(protocol.read_startup_message(reader.readexactly))
    assert startup is not None
    assert startup.protocol_version == protocol.PROTOCOL_VERSION_3
    assert startup.user == "obsl"
    assert startup.database == "__default__"


def test_read_startup_message_returns_none_on_ssl_request() -> None:
    body = struct.pack("!I", protocol.SSL_REQUEST_CODE)
    payload = struct.pack("!I", 4 + len(body)) + body
    reader = _ByteReader(payload)
    result = asyncio.run(protocol.read_startup_message(reader.readexactly))
    assert result is None


def test_read_startup_message_rejects_unknown_protocol() -> None:
    body = struct.pack("!I", 0xDEADBEEF)
    payload = struct.pack("!I", 4 + len(body)) + body
    reader = _ByteReader(payload)
    with pytest.raises(protocol.ProtocolError):
        asyncio.run(protocol.read_startup_message(reader.readexactly))
