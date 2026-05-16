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


# ---------------------------------------------------------------------------
# Extended query protocol (Step 4) — frame parsers and writers.
# ---------------------------------------------------------------------------


def test_parse_parse_decodes_statement_and_oids() -> None:
    body = (
        b"my_stmt\x00"
        + b"SELECT $1, $2\x00"
        + struct.pack("!H", 2)
        + struct.pack("!II", protocol.OID_TEXT, protocol.OID_INT4)
    )
    msg = protocol.parse_parse(body)
    assert msg.statement_name == "my_stmt"
    assert msg.query == "SELECT $1, $2"
    assert msg.param_oids == (protocol.OID_TEXT, protocol.OID_INT4)


def test_parse_parse_handles_unnamed_statement() -> None:
    body = b"\x00SELECT 1\x00" + struct.pack("!H", 0)
    msg = protocol.parse_parse(body)
    assert msg.statement_name == ""
    assert msg.query == "SELECT 1"
    assert msg.param_oids == ()


def test_parse_bind_decodes_values_and_formats() -> None:
    body = (
        b"p\x00s\x00"
        + struct.pack("!H", 2)  # 2 param formats
        + struct.pack("!HH", 0, 0)  # text, text
        + struct.pack("!H", 2)  # 2 params
        + struct.pack("!I", 3)
        + b"abc"
        + struct.pack("!i", -1)  # NULL second param
        + struct.pack("!H", 1)  # 1 result format
        + struct.pack("!H", 0)
    )
    msg = protocol.parse_bind(body)
    assert msg.portal_name == "p"
    assert msg.statement_name == "s"
    assert msg.param_formats == (0, 0)
    assert msg.param_values == (b"abc", None)
    assert msg.result_formats == (0,)


def test_parse_describe_target_validation() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_describe(b"X\x00")
    msg = protocol.parse_describe(b"P\x00")
    assert msg.target == b"P"
    assert msg.name == ""


def test_parse_execute_includes_max_rows() -> None:
    body = b"p\x00" + struct.pack("!I", 42)
    msg = protocol.parse_execute(body)
    assert msg.portal_name == "p"
    assert msg.max_rows == 42


def test_parse_close_distinguishes_statement_and_portal() -> None:
    stmt = protocol.parse_close(b"Smy_stmt\x00")
    portal = protocol.parse_close(b"Pportal\x00")
    assert stmt.target == b"S"
    assert stmt.name == "my_stmt"
    assert portal.target == b"P"


def test_build_parse_complete_is_empty_one_byte() -> None:
    frame = protocol.build_parse_complete()
    assert frame == b"1" + struct.pack("!I", 4)


def test_build_bind_complete_is_empty() -> None:
    assert protocol.build_bind_complete() == b"2" + struct.pack("!I", 4)


def test_build_close_complete_is_empty() -> None:
    assert protocol.build_close_complete() == b"3" + struct.pack("!I", 4)


def test_build_no_data_and_empty_query_response() -> None:
    assert protocol.build_no_data() == b"n" + struct.pack("!I", 4)
    assert protocol.build_empty_query_response() == b"I" + struct.pack("!I", 4)


def test_build_parameter_description_lists_oids() -> None:
    frame = protocol.build_parameter_description([protocol.OID_TEXT, protocol.OID_INT4])
    assert frame[:1] == b"t"
    payload = frame[5:]
    (n,) = struct.unpack("!H", payload[:2])
    assert n == 2
    oids = struct.unpack("!II", payload[2:10])
    assert oids == (protocol.OID_TEXT, protocol.OID_INT4)
