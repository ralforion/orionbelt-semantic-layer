"""Unit tests for the extended Postgres query protocol (Step 4)."""

from __future__ import annotations

import asyncio
import struct

import pytest

from orionbelt.pgwire import protocol
from orionbelt.pgwire.extended import (
    ExtendedSession,
    _split_simple_reply,
    substitute_parameters,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _select_one_reply() -> bytes:
    """A canned router reply for ``SELECT 1`` — matches canned.py output."""

    return (
        protocol.build_row_description([("?column?", protocol.OID_INT4)])
        + protocol.build_data_row(["1"])
        + protocol.build_command_complete("SELECT 1")
    )


def _two_row_reply() -> bytes:
    return (
        protocol.build_row_description([("col", protocol.OID_TEXT)])
        + protocol.build_data_row(["a"])
        + protocol.build_data_row(["b"])
        + protocol.build_command_complete("SELECT 2")
    )


def _error_reply() -> bytes:
    return protocol.build_error_response(severity="ERROR", code="42703", message="undefined column")


# ---------------------------------------------------------------------------
# Parameter substitution
# ---------------------------------------------------------------------------


def test_substitute_inlines_text_value() -> None:
    sql = substitute_parameters("SELECT * FROM t WHERE x = $1", (b"abc",), [0])
    assert sql == "SELECT * FROM t WHERE x = 'abc'"


def test_substitute_inlines_null() -> None:
    sql = substitute_parameters("SELECT $1", (None,), [0])
    assert sql == "SELECT NULL"


def test_substitute_escapes_single_quote() -> None:
    sql = substitute_parameters("SELECT $1", (b"O'Hara",), [0])
    assert sql == "SELECT 'O''Hara'"


def test_substitute_handles_multiple_placeholders() -> None:
    sql = substitute_parameters(
        "SELECT $1, $2, $3 FROM t WHERE y = $2",
        (b"a", b"b", b"c"),
        [0, 0, 0],
    )
    assert sql == "SELECT 'a', 'b', 'c' FROM t WHERE y = 'b'"


def test_substitute_skips_inside_single_quotes() -> None:
    sql = substitute_parameters(
        "SELECT '$1 is literal' AS note, $1",
        (b"val",),
        [0],
    )
    assert sql == "SELECT '$1 is literal' AS note, 'val'"


def test_substitute_skips_inside_double_quotes() -> None:
    sql = substitute_parameters(
        'SELECT "$1" AS "$1", $1',
        (b"val",),
        [0],
    )
    assert sql == 'SELECT "$1" AS "$1", \'val\''


def test_substitute_rejects_binary_format() -> None:
    from orionbelt.pgwire.extended import _BinaryParameterError

    with pytest.raises(_BinaryParameterError):
        substitute_parameters("SELECT $1", (b"\x00\x01",), [1])


def test_substitute_rejects_out_of_range_placeholder() -> None:
    from orionbelt.pgwire.extended import _BadParameterError

    with pytest.raises(_BadParameterError):
        substitute_parameters("SELECT $5", (b"x",), [0])


# ---------------------------------------------------------------------------
# Reply splitter
# ---------------------------------------------------------------------------


def test_split_simple_reply_decodes_row_data_command() -> None:
    reply = _split_simple_reply(_select_one_reply())
    assert reply.row_description.startswith(b"T")
    assert len(reply.data_rows) == 1
    assert reply.data_rows[0].startswith(b"D")
    assert reply.command_complete.startswith(b"C")
    assert not reply.is_error
    assert not reply.is_empty_query


def test_split_simple_reply_picks_up_error() -> None:
    reply = _split_simple_reply(_error_reply())
    assert reply.is_error
    assert reply.error.startswith(b"E")
    assert not reply.row_description
    assert reply.data_rows == ()


def test_split_simple_reply_empty_query() -> None:
    reply = _split_simple_reply(protocol.build_command_complete(""))
    assert reply.is_empty_query


# ---------------------------------------------------------------------------
# ExtendedSession lifecycle
# ---------------------------------------------------------------------------


def _make_session(reply_bytes: bytes) -> ExtendedSession:
    """ExtendedSession with a stub handler that always returns ``reply_bytes``."""

    async def handler(_sql: str, _db: str) -> bytes:
        return reply_bytes

    return ExtendedSession(handler=handler, database="")


def test_parse_complete() -> None:
    sess = _make_session(_select_one_reply())
    reply = sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 1", param_oids=()))
    assert _parse_frames(reply) == [(b"1", b"")]


def test_bind_complete_for_known_statement() -> None:
    sess = _make_session(_select_one_reply())
    sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 1", param_oids=()))
    reply = asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name="",
                param_formats=(),
                param_values=(),
                result_formats=(),
            )
        )
    )
    assert _parse_frames(reply) == [(b"2", b"")]


def test_describe_portal_returns_row_description() -> None:
    sess = _make_session(_select_one_reply())
    sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 1", param_oids=()))
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name="",
                param_formats=(),
                param_values=(),
                result_formats=(),
            )
        )
    )
    reply = sess.describe(protocol.DescribeMessage(target=b"P", name=""))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"T"]


def test_describe_statement_returns_param_description_and_no_data() -> None:
    sess = _make_session(_select_one_reply())
    sess.parse(
        protocol.ParseMessage(
            statement_name="s1", query="SELECT $1", param_oids=(protocol.OID_TEXT,)
        )
    )
    reply = sess.describe(protocol.DescribeMessage(target=b"S", name="s1"))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"t", b"n"]


def test_execute_replays_data_rows_and_command_complete() -> None:
    sess = _make_session(_two_row_reply())
    sess.parse(protocol.ParseMessage(statement_name="", query="SELECT col FROM t", param_oids=()))
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name="",
                param_formats=(),
                param_values=(),
                result_formats=(),
            )
        )
    )
    reply = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"D", b"D", b"C"]


def test_execute_returns_empty_query_response_for_blank_sql() -> None:
    # Router returns a bare CommandComplete with empty tag for whitespace.
    blank_reply = protocol.build_command_complete("")
    sess = _make_session(blank_reply)
    sess.parse(protocol.ParseMessage(statement_name="", query="", param_oids=()))
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name="",
                param_formats=(),
                param_values=(),
                result_formats=(),
            )
        )
    )
    reply = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"I"]


def test_execute_returns_cached_error_response() -> None:
    sess = _make_session(_error_reply())
    sess.parse(protocol.ParseMessage(statement_name="", query="oops", param_oids=()))
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name="",
                param_formats=(),
                param_values=(),
                result_formats=(),
            )
        )
    )
    reply = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    frames = _parse_frames(reply)
    assert frames[0][0] == b"E"


def test_close_statement_and_portal() -> None:
    sess = _make_session(_select_one_reply())
    sess.parse(protocol.ParseMessage(statement_name="s", query="SELECT 1", param_oids=()))
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="p",
                statement_name="s",
                param_formats=(),
                param_values=(),
                result_formats=(),
            )
        )
    )
    close_p = sess.close(protocol.CloseMessage(target=b"P", name="p"))
    close_s = sess.close(protocol.CloseMessage(target=b"S", name="s"))
    assert _parse_frames(close_p) == [(b"3", b"")]
    assert _parse_frames(close_s) == [(b"3", b"")]


def test_describe_missing_statement_returns_error() -> None:
    sess = _make_session(_select_one_reply())
    reply = sess.describe(protocol.DescribeMessage(target=b"S", name="missing"))
    assert reply.startswith(b"E")


def test_describe_missing_portal_returns_error() -> None:
    sess = _make_session(_select_one_reply())
    reply = sess.describe(protocol.DescribeMessage(target=b"P", name="missing"))
    assert reply.startswith(b"E")


def test_execute_missing_portal_returns_error() -> None:
    sess = _make_session(_select_one_reply())
    reply = sess.execute(protocol.ExecuteMessage(portal_name="missing", max_rows=0))
    assert reply.startswith(b"E")
