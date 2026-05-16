"""Integration tests for the pgwire surface.

Drives a live ``PgWireServer`` on an ephemeral port using raw asyncio
sockets. This avoids a hard dependency on psycopg/JDBC for the simple-
query cycle; client-library tests land alongside the extended-query
protocol in Step 4.

Step 1 (handshake + canned ``SELECT 1``) lives below.  Step 2 adds
end-to-end coverage that drives a :class:`SemanticRouter` through the
same socket — the router's translate/compile path is exercised for
real; execution is stubbed so the test doesn't need a live warehouse.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from typing import Any

import pytest

from orionbelt.pgwire import protocol
from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult
from orionbelt.service.session_manager import SessionManager
from tests.conftest import SAMPLE_MODEL_YAML


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


# ---------------------------------------------------------------------------
# Step 2: SemanticRouter end-to-end over a live socket.
# ---------------------------------------------------------------------------


@pytest.fixture
async def pgwire_with_router(monkeypatch: pytest.MonkeyPatch) -> PgWireServer:
    """Live server with a SemanticRouter handler bound to a loaded model.

    ``execute_sql`` is monkeypatched to return a deterministic stub —
    the test exercises the translate/compile/encode path; live database
    execution is covered by the vendor-specific integration suites.
    """

    mgr = SessionManager()
    store = mgr.get_or_create_named("commerce")
    store.load_model(SAMPLE_MODEL_YAML)

    def fake_execute(sql: str, **_: Any) -> ExecutionResult:
        return ExecutionResult(
            columns=[
                ColumnMeta(name="Customer Country", type_hint="string"),
                ColumnMeta(name="Total Revenue", type_hint="number"),
            ],
            raw_rows=[["DE", 1234], ["US", 9876]],
            row_count=2,
        )

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        max_connections=8,
        query_handler=router.handle,
    )
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        yield server
    finally:
        await server.stop()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve_task


async def test_semantic_query_returns_real_rows(pgwire_with_router: PgWireServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(_query_frame('SELECT "Customer Country", "Total Revenue" FROM commerce'))
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        assert tags == [b"T", b"D", b"D", b"C", b"Z"]

        # RowDescription advertises both columns.
        _, desc = reply[0]
        (n_cols,) = struct.unpack("!H", desc[:2])
        assert n_cols == 2

        # CommandComplete carries the row count.
        _, cmd = reply[3]
        assert cmd.startswith(b"SELECT 2")
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_catalog_probe_lists_loaded_model(pgwire_with_router: PgWireServer) -> None:
    """psql `\\dt`-style query surfaces the loaded model as a relation."""

    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        sql = (
            "SELECT n.nspname, c.relname, c.relkind "
            "FROM pg_catalog.pg_class c "
            "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r','p','') "
            "AND n.nspname NOT IN ('pg_catalog','information_schema') "
            "ORDER BY 1,2"
        )
        writer.write(_query_frame(sql))
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        # Expect RowDescription + at least one DataRow + CommandComplete + Z.
        assert tags[0] == b"T"
        assert b"D" in tags
        assert tags[-1] == b"Z"

        # At least one row, and one of them carries the model name.
        data_rows = [body for tag, body in reply if tag == b"D"]
        names_present = False
        for body in data_rows:
            if b"commerce" in body:
                names_present = True
                break
        assert names_present
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_show_server_version_returns_canned_string(
    pgwire_with_router: PgWireServer,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(_query_frame("SHOW server_version"))
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        assert tags == [b"T", b"D", b"C", b"Z"]
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# Step 4: extended-query protocol over a live socket.
# ---------------------------------------------------------------------------


def _parse_frame(buf: bytes) -> tuple[bytes, bytes]:
    """Wrap a body in tag + length for client → server frames."""

    tag, body = buf[:1], buf[1:]
    return tag, body


def _build_parse(stmt_name: str, query: str, oids: tuple[int, ...] = ()) -> bytes:
    payload = stmt_name.encode() + b"\x00" + query.encode() + b"\x00" + struct.pack("!H", len(oids))
    for oid in oids:
        payload += struct.pack("!I", oid)
    return b"P" + struct.pack("!I", 4 + len(payload)) + payload


def _build_bind(
    portal: str,
    stmt: str,
    values: list[bytes | None] | None = None,
) -> bytes:
    values = values or []
    payload = (
        portal.encode()
        + b"\x00"
        + stmt.encode()
        + b"\x00"
        + struct.pack("!H", 0)  # no format codes — defaults to text
        + struct.pack("!H", len(values))
    )
    for v in values:
        if v is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(v)) + v
    payload += struct.pack("!H", 0)  # no result format codes
    return b"B" + struct.pack("!I", 4 + len(payload)) + payload


def _build_describe(target: bytes, name: str) -> bytes:
    payload = target + name.encode() + b"\x00"
    return b"D" + struct.pack("!I", 4 + len(payload)) + payload


def _build_execute(portal: str, max_rows: int = 0) -> bytes:
    payload = portal.encode() + b"\x00" + struct.pack("!I", max_rows)
    return b"E" + struct.pack("!I", 4 + len(payload)) + payload


def _build_sync() -> bytes:
    return b"S" + struct.pack("!I", 4)


def _build_close(target: bytes, name: str) -> bytes:
    payload = target + name.encode() + b"\x00"
    return b"C" + struct.pack("!I", 4 + len(payload)) + payload


async def test_extended_query_select_one_round_trip(
    pgwire_with_router: PgWireServer,
) -> None:
    """Parse → Bind → Describe('P') → Execute → Sync returns one row."""

    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(
            _build_parse("", "SELECT 1", ())
            + _build_bind("", "", [])
            + _build_describe(b"P", "")
            + _build_execute("", 0)
            + _build_sync()
        )
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        # Expected ordering: ParseComplete, BindComplete, RowDescription,
        # DataRow, CommandComplete, ReadyForQuery.
        assert tags == [b"1", b"2", b"T", b"D", b"C", b"Z"]
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_extended_query_parameter_substitution(
    pgwire_with_router: PgWireServer,
) -> None:
    """Bind values flow through the parameter substituter into the SQL."""

    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        # The fake_execute in the fixture ignores the SQL; we just need
        # the substitution + protocol flow to complete cleanly.
        writer.write(
            _build_parse(
                "p1",
                "SELECT $1::text AS value",
                (protocol.OID_TEXT,),
            )
            + _build_bind("portal1", "p1", [b"hello"])
            + _build_execute("portal1", 0)
            + _build_sync()
        )
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        # ParseComplete, BindComplete, DataRow*, CommandComplete, RFQ.
        assert tags[0] == b"1"
        assert tags[1] == b"2"
        assert tags[-1] == b"Z"
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_extended_describe_statement_returns_param_desc_and_no_data(
    pgwire_with_router: PgWireServer,
) -> None:
    """Describe('S') before Bind responds with ParameterDescription + NoData."""

    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(
            _build_parse("s1", "SELECT $1", (protocol.OID_TEXT,))
            + _build_describe(b"S", "s1")
            + _build_sync()
        )
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        # ParseComplete, ParameterDescription, NoData, RFQ.
        assert tags == [b"1", b"t", b"n", b"Z"]
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_extended_error_then_sync_recovers_session(
    pgwire_with_router: PgWireServer,
) -> None:
    """An error in extended mode enters skip-until-Sync; Sync restores."""

    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        writer.write(_startup_payload({"user": "obsl", "database": "commerce"}))
        await writer.drain()
        await _drain_until_ready(reader)

        # Bind a statement that doesn't exist → ErrorResponse.
        writer.write(_build_bind("", "missing", []) + _build_execute("", 0) + _build_sync())
        await writer.drain()
        reply = await _drain_until_ready(reader)
        tags = [t for t, _ in reply]
        assert tags[0] == b"E"
        # The Execute message issued mid-error is dropped silently; the
        # final RFQ from Sync still appears.
        assert tags[-1] == b"Z"
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_unknown_database_returns_3d000(pgwire_with_router: PgWireServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", pgwire_with_router.bound_port)
    try:
        # Database name doesn't match any session and __default__ is
        # empty — router should return an undefined_database error.
        writer.write(_startup_payload({"user": "obsl", "database": "ghost"}))
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(_query_frame('SELECT "Customer Country" FROM ghost'))
        await writer.drain()
        reply = await _drain_until_ready(reader)
        assert reply[0][0] == b"E"
        assert b"C3D000\x00" in reply[0][1]
    finally:
        writer.write(_terminate_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
