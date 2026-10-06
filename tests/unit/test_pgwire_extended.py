"""Unit tests for the extended Postgres query protocol (Step 4)."""

from __future__ import annotations

import asyncio
import decimal
import struct

import duckdb
import pytest

from orionbelt.pgwire import protocol
from orionbelt.pgwire.extended import (
    ExtendedSession,
    _BadParameterError,
    _decode_binary_param,
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
    assert (
        substitute_parameters("x $1", (b"  -7 ",), [0], param_oids=(23,)) == "x CAST(-7 AS INTEGER)"
    )
    assert (
        substitute_parameters("x $1", (b"3.14",), [0], param_oids=(1700,))
        == "x CAST(3.14 AS DECIMAL(3, 2))"
    )
    assert (
        substitute_parameters("x $1", (b"1e3",), [0], param_oids=(701,)) == "x CAST(1E+3 AS DOUBLE)"
    )
    # Fractional edge forms Postgres accepts.
    assert (
        substitute_parameters("x $1", (b".5",), [0], param_oids=(1700,))
        == "x CAST(0.5 AS DECIMAL(2, 1))"
    )
    assert (
        substitute_parameters("x $1", (b"-2.5E-3",), [0], param_oids=(701,))
        == "x CAST(-0.0025 AS DOUBLE)"
    )


def test_substitute_numeric_text_rejects_nonstandard_forms() -> None:
    """Underscores / unicode digits / hex are rejected for float+numeric OIDs
    (matching Postgres text grammar), not silently normalized by Decimal."""
    from orionbelt.pgwire.extended import _BadParameterError

    for oid in (700, 701, 1700):
        for payload in (b"1_000", b"1__0", "１２".encode(), "٣".encode(), b"0x10"):
            with pytest.raises(_BadParameterError):
                substitute_parameters("x $1", (payload,), [0], param_oids=(oid,))


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


@pytest.mark.parametrize(
    ("sql", "value", "oid", "expected"),
    [
        # Exact NUMERIC, not a DOUBLE round trip (review of #513).
        (
            "SELECT CAST($1 AS DECIMAL(38,2))",
            b"9007199254740993.00",
            1700,
            decimal.Decimal("9007199254740993.00"),
        ),
        # A DATE parameter compares as a date.
        ("SELECT DATE '2024-01-02' > $1", b"2024-01-01", 1082, True),
        # OID 0 leaves the type to the context, a NULL included.
        ("SELECT DATE '2024-01-02' > $1", b"2024-01-01", 0, True),
        ("SELECT DATE '2024-01-02' > $1", None, 0, None),
    ],
)
def test_bound_parameters_keep_value_and_comparability(
    sql: str, value: bytes | None, oid: int, expected: object
) -> None:
    rendered = substitute_parameters(sql, (value,), [0], param_oids=(oid,))
    assert duckdb.connect().execute(rendered).fetchone() == (expected,)


def test_float4_binds_as_the_real_it_denotes() -> None:
    assert (
        substitute_parameters("x $1", (b"0.1",), [0], param_oids=(700,))
        == "x CAST(0.10000000149011612 AS REAL)"
    )


def test_float4_out_of_range_is_a_parameter_error() -> None:
    with pytest.raises(_BadParameterError, match="out of range"):
        substitute_parameters("x $1", (b"1e39",), [0], param_oids=(700,))


def test_uuid_binds_in_canonical_form_and_refuses_garbage() -> None:
    assert (
        substitute_parameters(
            "x $1", (b"A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11",), [0], param_oids=(2950,)
        )
        == "x CAST('a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' AS UUID)"
    )
    with pytest.raises(_BadParameterError, match="uuid"):
        substitute_parameters("x $1", (b"not-a-uuid",), [0], param_oids=(2950,))


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
    assert sql == "INSERT INTO t VALUES (CAST(42 AS INTEGER))"


def test_substitute_decodes_binary_int2_int8() -> None:
    sql = substitute_parameters(
        "SELECT $1, $2",
        (struct.pack("!h", -7), struct.pack("!q", 1_000_000_000_000)),
        [1, 1],
        param_oids=(21, 20),  # INT2, INT8
    )
    assert sql == "SELECT CAST(-7 AS SMALLINT), CAST(1000000000000 AS BIGINT)"


def test_substitute_decodes_binary_float8() -> None:
    sql = substitute_parameters("SELECT $1", (struct.pack("!d", 3.5),), [1], param_oids=(701,))
    assert sql == "SELECT CAST(3.5 AS DOUBLE)"


def test_substitute_decodes_binary_bool() -> None:
    assert (
        substitute_parameters("SELECT $1", (b"\x01",), [1], param_oids=(16,))
        == "SELECT CAST(TRUE AS BOOLEAN)"
    )
    assert (
        substitute_parameters("SELECT $1", (b"\x00",), [1], param_oids=(16,))
        == "SELECT CAST(FALSE AS BOOLEAN)"
    )


def test_substitute_decodes_binary_text() -> None:
    sql = substitute_parameters("SELECT $1", (b"hi'there",), [1], param_oids=(25,))
    assert sql == "SELECT CAST('hi''there' AS VARCHAR)"


# Postgres 16 ``*_send`` output: psycopg 3 binds dates, datetimes, timedeltas,
# decimals and UUIDs in these binary formats by default; pgjdbc binds a
# BigDecimal as binary NUMERIC on a server-prepared statement.
@pytest.mark.parametrize(
    ("oid", "pg_hex", "literal"),
    [
        (1082, "00002279", "'2024-02-29'"),
        (1083, "00000008cd15db20", "'10:30:00.500000'"),
        (1114, "0003001dc047efc0", "'2026-10-05 21:58:30.123456'"),
        (1184, "0002b0de36ada800", "'2024-01-01 10:00:00+00:00'"),
        (1186, "00000000003567e00000000200000001", "'1 months 2 days 3500000 microseconds'"),
        (2950, "a0eebc999c0b4ef8bb6d6bb9bd380a11", "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'"),
        (1700, "0004000300000002232f07c8156203e1", "9007199254740993.00"),
        (
            1700,
            "000a000900000000000c0d801ed204d2162e23340d801ed204d2162e",
            "12345678901234567890123456789012345678",
        ),
    ],
)
def test_binary_parameters_decode_like_postgres(oid: int, pg_hex: str, literal: str) -> None:
    assert _decode_binary_param(bytes.fromhex(pg_hex), oid) == literal


@pytest.mark.parametrize(
    ("oid", "pg_hex"),
    [
        (1700, "00000000c0000000"),  # NaN
        (1082, "7fffffff"),  # infinity
    ],
)
def test_non_finite_binary_parameters_are_refused(oid: int, pg_hex: str) -> None:
    with pytest.raises(_BadParameterError):
        _decode_binary_param(bytes.fromhex(pg_hex), oid)


@pytest.mark.parametrize(
    ("micros", "literal"),
    [
        # int32 extremes are ordinary timestamps near 2000 (review of #516).
        (2**31 - 1, "'2000-01-01 00:35:47.483647'"),
        (-(2**31), "'1999-12-31 23:24:12.516352'"),
    ],
)
def test_int32_extreme_timestamps_are_not_infinity(micros: int, literal: str) -> None:
    assert _decode_binary_param(struct.pack("!q", micros), 1114) == literal


def test_infinite_timestamp_and_oversized_numeric_are_refused() -> None:
    with pytest.raises(_BadParameterError, match="Infinite"):
        _decode_binary_param(struct.pack("!q", 2**63 - 1), 1114)
    # weight 300 (10000 ** 300, 1200 digits): past the accepted width.
    with pytest.raises(_BadParameterError, match="out of range"):
        _decode_binary_param(struct.pack("!hhHHH", 1, 300, 0, 0, 1), 1700)


def test_describe_lets_the_context_type_an_unspecified_parameter() -> None:
    """pgjdbc binds a date unspecified: ``DATE '...' > $1`` must still describe."""
    text_null = "SELECT DATE '2024-01-02' > CAST(NULL AS VARCHAR)"
    sess, seen = _recording_session({text_null: _error_reply()}, _two_row_reply())
    _parse(sess, "s1", "SELECT DATE '2024-01-02' > $1", (0,))
    assert _describe(sess, b"S", "s1") == [b"t", b"T"]
    assert seen == [text_null, "SELECT DATE '2024-01-02' > NULL"]


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
    reply = asyncio.run(sess.describe(protocol.DescribeMessage(target=b"P", name="")))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"T"]


def _recording_session(
    reply_for: dict[str, bytes], default: bytes
) -> tuple[ExtendedSession, list[str]]:
    """ExtendedSession whose handler records each SQL and answers by exact match."""

    seen: list[str] = []

    async def handler(sql: str, _db: str, **_kwargs: object) -> bytes:
        seen.append(sql)
        return reply_for.get(sql, default)

    return ExtendedSession(handler=handler, database=""), seen


def _parse(sess: ExtendedSession, name: str, sql: str, oids: tuple[int, ...]) -> None:
    asyncio.run(sess.parse(protocol.ParseMessage(statement_name=name, query=sql, param_oids=oids)))


def _bind(sess: ExtendedSession, stmt: str, values: tuple[bytes | None, ...]) -> None:
    asyncio.run(
        sess.bind(
            protocol.BindMessage(
                portal_name="",
                statement_name=stmt,
                param_formats=(),
                param_values=values,
                result_formats=(),
            )
        )
    )


def _describe(sess: ExtendedSession, target: bytes, name: str) -> list[bytes]:
    reply = asyncio.run(sess.describe(protocol.DescribeMessage(target=target, name=name)))
    return [t for t, _ in _parse_frames(reply)]


def test_describe_parameterised_statement_runs_it_with_null_parameters() -> None:
    """pgjdbc ``prepareThreshold=-1``: Describe('S') before any Bind (#511)."""
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", "SELECT col FROM t WHERE col = $1", (protocol.OID_TEXT,))
    assert seen == []  # parameterised: nothing ran at Parse
    assert _describe(sess, b"S", "s1") == [b"t", b"T"]
    assert seen == ["SELECT col FROM t WHERE col = CAST(NULL AS VARCHAR)"]


def test_describe_parameterised_falls_back_to_shape_only() -> None:
    """OBSQL rejects a NULL comparison; the WHERE-less ``LIMIT 0`` variant answers."""
    sess, seen = _recording_session(
        {'SELECT "a" FROM m WHERE "a" = CAST(NULL AS VARCHAR) LIMIT 5': _error_reply()},
        _two_row_reply(),
    )
    _parse(sess, "s1", 'SELECT "a" FROM m WHERE "a" = $1 LIMIT 5', (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "s1") == [b"t", b"T"]
    assert seen[-1] == 'SELECT "a" FROM m LIMIT 0'


def test_describe_parameterised_keeps_no_data_when_nothing_works() -> None:
    sess, _ = _recording_session({}, _error_reply())
    _parse(sess, "s1", "SELECT $1", (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "s1") == [b"t", b"n"]


def test_describe_parameterised_never_runs_a_write() -> None:
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", "INSERT INTO t VALUES ($1)", (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "s1") == [b"t", b"n"]
    assert seen == []


def test_describe_types_each_null_like_bind_renders_it() -> None:
    """A TEXT parameter must not describe as a number (review of #513)."""
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", "SELECT $1, $2", (protocol.OID_TEXT, 23))
    _describe(sess, b"S", "s1")
    assert seen == ["SELECT CAST(NULL AS VARCHAR), CAST(NULL AS INTEGER)"]


def test_describe_ignores_placeholders_inside_literals_and_comments() -> None:
    """``'$1000000'`` once sized a list by that number."""
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", "SELECT '$1000000' /* $99 */, $1", (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "s1") == [b"t", b"T"]
    assert seen == ["SELECT '$1000000' /* $99 */, CAST(NULL AS VARCHAR)"]


def test_describe_refuses_a_placeholder_past_the_parameter_limit() -> None:
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", "SELECT $70000", ())
    assert _describe(sess, b"S", "s1") == [b"t", b"n"]
    assert seen == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a INTO TEMP t FROM x WHERE b = $1",
        "WITH d AS (DELETE FROM t WHERE id = $1 RETURNING *) SELECT * FROM d",
        "EXPLAIN ANALYZE SELECT a FROM t WHERE b = $1",
    ],
)
def test_describe_never_runs_a_write_shaped_read(sql: str) -> None:
    """SELECT INTO ran at Describe, so Bind then failed with "already exists"."""
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", sql, (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "s1") == [b"t", b"n"]
    assert seen == []


def test_parse_does_not_pre_run_a_select_into() -> None:
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "", "SELECT 1 AS a INTO TEMP t", ())
    assert seen == []


def test_a_quoted_identifier_is_not_a_write() -> None:
    sess, seen = _recording_session({}, _two_row_reply())
    _parse(sess, "s1", 'SELECT "Insert Date" FROM m WHERE "Update" = $1', (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "s1") == [b"t", b"T"]


def _reply_with(oid: int, fmt: int, value: str | bytes) -> bytes:
    return (
        protocol.build_row_description([("c", oid, fmt)])
        + protocol.build_data_row([value])
        + protocol.build_command_complete("SELECT 1")
    )


def test_execute_refuses_a_result_the_held_description_misreads() -> None:
    """Described as FLOAT8, bound as TEXT: refuse rather than misdecode."""
    described = "SELECT CAST(NULL AS VARCHAR)"
    sess, _ = _recording_session(
        {described: _reply_with(701, 0, "")}, _reply_with(25, 0, "abcdefgh")
    )
    _parse(sess, "S_1", "SELECT $1", (protocol.OID_TEXT,))
    assert _describe(sess, b"S", "S_1") == [b"t", b"T"]
    _bind(sess, "S_1", (b"abcdefgh",))
    reply = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"E"]
    assert b"cached plan must not change result type" in frames[0][1]


def test_a_fresh_describe_portal_clears_the_mismatch() -> None:
    """A client that describes the portal gets the right types and is served."""
    described = "SELECT CAST(NULL AS VARCHAR)"
    sess, _ = _recording_session(
        {described: _reply_with(701, 0, "")}, _reply_with(25, 0, "abcdefgh")
    )
    _parse(sess, "S_1", "SELECT $1", (protocol.OID_TEXT,))
    _describe(sess, b"S", "S_1")
    _bind(sess, "S_1", (b"abcdefgh",))
    assert _describe(sess, b"P", "") == [b"T"]
    reply = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    assert [t for t, _ in _parse_frames(reply)] == [b"D", b"C"]


def test_reused_statement_does_not_repeat_row_description() -> None:
    """pgjdbc from the 6th run: Bind + Execute only on a described statement (#511)."""
    sess, _ = _recording_session({}, _two_row_reply())
    _parse(sess, "S_1", "SELECT col FROM t WHERE col = $1", (protocol.OID_TEXT,))
    _bind(sess, "S_1", (b"a",))
    assert _describe(sess, b"P", "") == [b"T"]
    first = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    assert [t for t, _ in _parse_frames(first)] == [b"D", b"D", b"C"]
    _bind(sess, "S_1", (b"b",))
    again = sess.execute(protocol.ExecuteMessage(portal_name="", max_rows=0))
    assert [t for t, _ in _parse_frames(again)] == [b"D", b"D", b"C"]


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
    reply = asyncio.run(sess.describe(protocol.DescribeMessage(target=b"S", name="missing")))
    assert reply.startswith(b"E")


def test_describe_missing_portal_returns_error() -> None:
    sess = _make_session(_select_one_reply())
    reply = asyncio.run(sess.describe(protocol.DescribeMessage(target=b"P", name="missing")))
    assert reply.startswith(b"E")


def test_execute_missing_portal_returns_error() -> None:
    sess = _make_session(_select_one_reply())
    reply = sess.execute(protocol.ExecuteMessage(portal_name="missing", max_rows=0))
    assert reply.startswith(b"E")
