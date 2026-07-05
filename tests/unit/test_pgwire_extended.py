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


def test_substitute_numeric_text_rejects_injection() -> None:
    """A numeric-typed text param that carries SQL syntax is rejected, not spliced."""
    from orionbelt.pgwire.extended import _BadParameterError

    with pytest.raises(_BadParameterError):
        substitute_parameters(
            'SELECT * FROM m WHERE "Total Revenue" > $1',
            (b"0 AND \"Customer Country\" = 'US'",),
            [0],
            param_oids=(23,),  # INT4
        )


def test_substitute_numeric_text_canonicalizes() -> None:
    """Valid numeric text params render as canonical numeric literals."""
    assert substitute_parameters("x $1", (b"  -7 ",), [0], param_oids=(23,)) == "x -7"
    assert substitute_parameters("x $1", (b"3.14",), [0], param_oids=(1700,)) == "x 3.14"
    assert substitute_parameters("x $1", (b"1e3",), [0], param_oids=(701,)) == "x 1E+3"


def test_substitute_numeric_text_rejects_non_finite() -> None:
    from orionbelt.pgwire.extended import _BadParameterError

    for payload in (b"NaN", b"Infinity", b"-Infinity"):
        with pytest.raises(_BadParameterError):
            substitute_parameters("x $1", (payload,), [0], param_oids=(1700,))


def test_substitute_skips_inside_line_comment() -> None:
    sql = substitute_parameters("SELECT 1 -- $1\nWHERE x = $1", (b"v",), [0])
    assert sql == "SELECT 1 -- $1\nWHERE x = 'v'"


def test_substitute_skips_inside_block_comment() -> None:
    sql = substitute_parameters("SELECT /* $1 nested /* $1 */ */ $1", (b"v",), [0])
    assert sql == "SELECT /* $1 nested /* $1 */ */ 'v'"


def test_substitute_skips_inside_dollar_quote() -> None:
    assert substitute_parameters("SELECT $$ $1 $$, $1", (b"v",), [0]) == "SELECT $$ $1 $$, 'v'"
    assert (
        substitute_parameters("SELECT $tag$ $1 $tag$, $1", (b"v",), [0])
        == "SELECT $tag$ $1 $tag$, 'v'"
    )


def test_substitute_dollar_digit_is_placeholder_not_tag() -> None:
    """``$1`` is a placeholder even next to a stray ``$`` (digit-led tags are invalid)."""
    assert substitute_parameters("SELECT $1$", (b"v",), [0]) == "SELECT 'v'$"


def test_substitute_rejects_binary_format_for_unknown_oid() -> None:
    """Unknown binary OID still errors — we only decode a small allow-list."""
    from orionbelt.pgwire.extended import _BinaryParameterError

    with pytest.raises(_BinaryParameterError):
        # OID 0 (unspecified) + binary format → not in the decode set.
        substitute_parameters("SELECT $1", (b"\x00\x01",), [1], param_oids=(0,))


def test_substitute_decodes_binary_int4() -> None:
    """Tableau's connect-check INSERTs INT4 binary — must inline as int literal."""
    sql = substitute_parameters(
        "INSERT INTO t VALUES ($1)",
        (struct.pack("!i", 42),),
        [1],
        param_oids=(23,),  # OID_INT4
    )
    assert sql == "INSERT INTO t VALUES (42)"


def test_substitute_decodes_binary_int2_int8() -> None:
    sql = substitute_parameters(
        "SELECT $1, $2",
        (struct.pack("!h", -7), struct.pack("!q", 1_000_000_000_000)),
        [1, 1],
        param_oids=(21, 20),  # INT2, INT8
    )
    assert sql == "SELECT -7, 1000000000000"


def test_substitute_decodes_binary_float8() -> None:
    sql = substitute_parameters("SELECT $1", (struct.pack("!d", 3.5),), [1], param_oids=(701,))
    assert sql == "SELECT 3.5"


def test_substitute_decodes_binary_bool() -> None:
    assert substitute_parameters("SELECT $1", (b"\x01",), [1], param_oids=(16,)) == "SELECT TRUE"
    assert substitute_parameters("SELECT $1", (b"\x00",), [1], param_oids=(16,)) == "SELECT FALSE"


def test_substitute_decodes_binary_text() -> None:
    sql = substitute_parameters("SELECT $1", (b"hi'there",), [1], param_oids=(25,))
    assert sql == "SELECT 'hi''there'"


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

    async def handler(_sql: str, _db: str, **_kwargs: object) -> bytes:
        # Accept any kwargs (e.g. ``result_formats``) the Bind path
        # passes through; this stub ignores them.
        return reply_bytes

    return ExtendedSession(handler=handler, database="")


def test_parse_complete() -> None:
    sess = _make_session(_select_one_reply())
    reply = asyncio.run(
        sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 1", param_oids=()))
    )
    assert _parse_frames(reply) == [(b"1", b"")]


def test_bind_complete_for_known_statement() -> None:
    sess = _make_session(_select_one_reply())
    asyncio.run(
        sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 1", param_oids=()))
    )
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
    asyncio.run(
        sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 1", param_oids=()))
    )
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
    asyncio.run(
        sess.parse(
            protocol.ParseMessage(
                statement_name="s1", query="SELECT $1", param_oids=(protocol.OID_TEXT,)
            )
        )
    )
    reply = sess.describe(protocol.DescribeMessage(target=b"S", name="s1"))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"t", b"n"]


def test_execute_replays_data_rows_and_command_complete() -> None:
    sess = _make_session(_two_row_reply())
    asyncio.run(
        sess.parse(
            protocol.ParseMessage(statement_name="", query="SELECT col FROM t", param_oids=())
        )
    )
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
    # Execute prepends RowDescription when Describe('P') wasn't called
    # (JDBC fast-path / Tableau compatibility). The data frames follow.
    assert [t for t, _ in frames] == [b"T", b"D", b"D", b"C"]


def test_bind_re_runs_handler_with_requested_result_formats() -> None:
    """Bind passes its ``result_formats`` through to the handler so the
    DataRow bytes are encoded matching what the client asked for.
    pgjdbc reads ``Bind.result_formats`` to decide how to parse each
    column — sending text bytes when binary was requested makes pgjdbc
    throw ``Index 7 out of bounds for length 7`` reading 8 bytes from
    a 7-char text payload.
    """

    text_reply = (
        protocol.build_row_description([("n", protocol.OID_INT4, 0)])
        + protocol.build_data_row(["42"])
        + protocol.build_command_complete("SELECT 1")
    )
    binary_reply = (
        protocol.build_row_description([("n", protocol.OID_INT4, 1)])
        + protocol.build_data_row([b"\x00\x00\x00\x2a"])
        + protocol.build_command_complete("SELECT 1")
    )
    calls: list[tuple[int, ...]] = []

    async def handler(_sql: str, _db: str, *, result_formats: tuple[int, ...] = ()) -> bytes:
        calls.append(result_formats)
        return binary_reply if result_formats and any(result_formats) else text_reply

    sess = ExtendedSession(handler=handler, database="")
    asyncio.run(
        sess.parse(protocol.ParseMessage(statement_name="", query="SELECT 42", param_oids=()))
    )
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name="",
                param_formats=(),
                param_values=(),
                result_formats=(1,),
            )
        )
    )
    # Handler was called twice: preexec (no formats) and Bind (binary).
    assert () in calls
    assert (1,) in calls


def test_execute_returns_empty_query_response_for_blank_sql() -> None:
    # Router returns a bare CommandComplete with empty tag for whitespace.
    blank_reply = protocol.build_command_complete("")
    sess = _make_session(blank_reply)
    asyncio.run(sess.parse(protocol.ParseMessage(statement_name="", query="", param_oids=())))
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
    asyncio.run(sess.parse(protocol.ParseMessage(statement_name="", query="oops", param_oids=())))
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
    asyncio.run(
        sess.parse(protocol.ParseMessage(statement_name="s", query="SELECT 1", param_oids=()))
    )
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
