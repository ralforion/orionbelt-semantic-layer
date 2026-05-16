"""Unit tests for pgwire/canned.py — protocol-level probe replies."""

from __future__ import annotations

import struct

from orionbelt.pgwire import protocol
from orionbelt.pgwire.canned import match_canned


def _parse_frames(blob: bytes) -> list[tuple[bytes, bytes]]:
    frames: list[tuple[bytes, bytes]] = []
    offset = 0
    while offset < len(blob):
        tag = blob[offset : offset + 1]
        (length,) = struct.unpack("!I", blob[offset + 1 : offset + 5])
        body = blob[offset + 5 : offset + 1 + length]
        frames.append((tag, body))
        offset += 1 + length
    return frames


def test_select_one_returns_int4_row() -> None:
    reply = match_canned("SELECT 1")
    assert reply is not None
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"T", b"D", b"C"]
    # RowDescription advertises a single column with OID 23 (int4).
    _, desc = frames[0]
    (n_cols,) = struct.unpack("!H", desc[:2])
    assert n_cols == 1
    name, rest = desc[2:].split(b"\x00", 1)
    assert name == b"?column?"
    _, _, oid, *_ = struct.unpack("!IhIhih", rest[:18])
    assert oid == protocol.OID_INT4


def test_select_version_returns_postgres_flavored_string() -> None:
    reply = match_canned("SELECT version()")
    assert reply is not None
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"T", b"D", b"C"]
    _, data_row = frames[1]
    (n_vals,) = struct.unpack("!H", data_row[:2])
    assert n_vals == 1
    (col_len,) = struct.unpack("!I", data_row[2:6])
    value = data_row[6 : 6 + col_len].decode()
    assert "PostgreSQL" in value
    assert "OrionBelt" in value


def test_show_server_version_uses_postgres_value() -> None:
    reply = match_canned("SHOW server_version")
    assert reply is not None
    frames = _parse_frames(reply)
    _, data_row = frames[1]
    (col_len,) = struct.unpack("!I", data_row[2:6])
    value = data_row[6 : 6 + col_len].decode()
    assert value.startswith("15.0")


def test_show_unknown_param_returns_empty_string() -> None:
    reply = match_canned("SHOW nonexistent_param")
    assert reply is not None
    frames = _parse_frames(reply)
    _, data_row = frames[1]
    (col_len,) = struct.unpack("!I", data_row[2:6])
    assert col_len == 0


def test_set_is_no_op() -> None:
    reply = match_canned("SET extra_float_digits = 3")
    assert reply is not None
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"C"]
    assert frames[0][1] == b"SET\x00"


def test_transaction_wrappers_accepted() -> None:
    for sql, tag_expected in [
        ("BEGIN", b"BEGIN\x00"),
        ("START TRANSACTION", b"BEGIN\x00"),
        ("COMMIT", b"COMMIT\x00"),
        ("END", b"COMMIT\x00"),
        ("ROLLBACK", b"ROLLBACK\x00"),
        ("SAVEPOINT a", b"SAVEPOINT\x00"),
        ("RELEASE SAVEPOINT a", b"SAVEPOINT\x00"),
    ]:
        reply = match_canned(sql)
        assert reply is not None, f"no canned reply for {sql!r}"
        frames = _parse_frames(reply)
        assert [t for t, _ in frames] == [b"C"]
        assert frames[0][1] == tag_expected, sql


def test_unknown_query_returns_none() -> None:
    assert match_canned("SELECT * FROM commerce") is None
    assert match_canned("SELECT pg_table_is_visible(1)") is None


def test_empty_query_returns_empty_command_complete() -> None:
    reply = match_canned("   ;  ")
    assert reply is not None
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"C"]
    assert frames[0][1] == b"\x00"
