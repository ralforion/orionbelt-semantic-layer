"""Postgres v3 wire-protocol frame readers and writers.

Pure I/O helpers — no business logic. The protocol is described in
PostgreSQL docs §52. Step 1 implements only the messages the
``SELECT 1`` happy path needs:

* Reads  — StartupMessage, Query (Q), Terminate (X), SSLRequest (peek)
* Writes — AuthenticationOk, ParameterStatus, BackendKeyData,
           ReadyForQuery, RowDescription, DataRow, CommandComplete,
           ErrorResponse, NoticeResponse

Subsequent steps grow this module; keep additions framework-free so the
server loop stays the only place that owns sockets.
"""

from __future__ import annotations

import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

ReadExactly = Callable[[int], Awaitable[bytes]]

# Postgres protocol version 3.0 == 0x00030000.
PROTOCOL_VERSION_3: Final[int] = 196608

# SSLRequest sentinel — Postgres clients negotiate TLS by sending an
# 8-byte packet with this magic int as the protocol version slot.
SSL_REQUEST_CODE: Final[int] = 80877103

# Common type OIDs (subset, full mapping lives in pgwire/types.py later).
OID_TEXT: Final[int] = 25
OID_INT4: Final[int] = 23

# Transaction status flags returned in ReadyForQuery.
TX_IDLE: Final[bytes] = b"I"


class ProtocolError(Exception):
    """Raised on malformed or unsupported wire frames."""


@dataclass(frozen=True)
class StartupMessage:
    """Parsed StartupMessage payload."""

    protocol_version: int
    parameters: dict[str, str]

    @property
    def user(self) -> str:
        return self.parameters.get("user", "")

    @property
    def database(self) -> str:
        return self.parameters.get("database", self.user)


@dataclass(frozen=True)
class QueryMessage:
    """Simple-query Q frame payload."""

    sql: str


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


async def read_startup_message(reader_read_exactly: ReadExactly) -> StartupMessage | None:
    """Read the initial StartupMessage / SSLRequest from a client.

    ``reader_read_exactly`` is any awaitable accepting an int byte count
    (asyncio.StreamReader.readexactly).  Returns ``None`` when the client
    sent an SSLRequest — caller responds with single byte ``N`` (reject)
    and re-invokes this function for the real StartupMessage.
    """

    length_bytes = await reader_read_exactly(4)
    (length,) = struct.unpack("!I", length_bytes)
    if length < 8 or length > 10_000:
        raise ProtocolError(f"Startup length out of bounds: {length}")
    body = await reader_read_exactly(length - 4)
    (code,) = struct.unpack("!I", body[:4])

    if code == SSL_REQUEST_CODE:
        return None

    if code != PROTOCOL_VERSION_3:
        raise ProtocolError(f"Unsupported protocol version: {code:#x}")

    # Body is a series of NUL-terminated UTF-8 key/value pairs followed
    # by a final empty key.
    params: dict[str, str] = {}
    parts = body[4:].split(b"\x00")
    # Trailing empty element from the final NUL.
    i = 0
    while i + 1 < len(parts):
        key = parts[i].decode("utf-8", errors="replace")
        if not key:
            break
        value = parts[i + 1].decode("utf-8", errors="replace")
        params[key] = value
        i += 2
    return StartupMessage(protocol_version=code, parameters=params)


async def read_message(reader_read_exactly: ReadExactly) -> tuple[bytes, bytes]:
    """Read a single tagged message frame.

    Returns ``(tag, body)`` where ``tag`` is the 1-byte message type and
    ``body`` excludes the 4-byte length prefix.  Caller dispatches on
    ``tag``.
    """

    tag = await reader_read_exactly(1)
    length_bytes = await reader_read_exactly(4)
    (length,) = struct.unpack("!I", length_bytes)
    if length < 4:
        raise ProtocolError(f"Message length too small: {length}")
    body = await reader_read_exactly(length - 4) if length > 4 else b""
    return tag, body


def parse_query(body: bytes) -> QueryMessage:
    """Parse the body of a Simple Query ``Q`` frame."""

    if not body.endswith(b"\x00"):
        raise ProtocolError("Query body not NUL-terminated")
    return QueryMessage(sql=body[:-1].decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Writers — each returns bytes; caller writes to the socket.
# ---------------------------------------------------------------------------


def _frame(tag: bytes, payload: bytes) -> bytes:
    """Wrap ``payload`` in a tagged Postgres frame."""

    return tag + struct.pack("!I", 4 + len(payload)) + payload


def build_authentication_ok() -> bytes:
    return _frame(b"R", struct.pack("!I", 0))


def build_parameter_status(name: str, value: str) -> bytes:
    payload = name.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
    return _frame(b"S", payload)


def build_backend_key_data(pid: int, secret: int) -> bytes:
    return _frame(b"K", struct.pack("!II", pid, secret))


def build_ready_for_query(status: bytes = TX_IDLE) -> bytes:
    if len(status) != 1:
        raise ValueError("ReadyForQuery status must be a single byte")
    return _frame(b"Z", status)


def build_row_description(columns: list[tuple[str, int]]) -> bytes:
    """RowDescription frame.

    ``columns`` is a list of ``(name, type_oid)`` pairs. Type-size /
    type-modifier / format columns are filled with sensible defaults for
    text-format results — Step 1 only emits text.
    """

    payload = struct.pack("!H", len(columns))
    for name, oid in columns:
        payload += name.encode("utf-8") + b"\x00"
        # table_oid, column_attr, type_oid, type_size, type_modifier, format_code
        payload += struct.pack("!IhIhih", 0, 0, oid, -1, -1, 0)
    return _frame(b"T", payload)


def build_data_row(values: list[str | None]) -> bytes:
    payload = struct.pack("!H", len(values))
    for value in values:
        if value is None:
            payload += struct.pack("!i", -1)
        else:
            encoded = value.encode("utf-8")
            payload += struct.pack("!I", len(encoded)) + encoded
    return _frame(b"D", payload)


def build_command_complete(tag: str) -> bytes:
    return _frame(b"C", tag.encode("utf-8") + b"\x00")


def build_error_response(*, severity: str, code: str, message: str) -> bytes:
    """ErrorResponse frame.

    Postgres uses SQLSTATE codes; ``XX000`` (internal error) is a safe
    catch-all when we don't have a more specific class. The router will
    map OBSL ``ErrorCode`` values onto SQLSTATEs in later steps.
    """

    fields = (
        b"S"
        + severity.encode("ascii")
        + b"\x00"
        + b"V"
        + severity.encode("ascii")
        + b"\x00"
        + b"C"
        + code.encode("ascii")
        + b"\x00"
        + b"M"
        + message.encode("utf-8")
        + b"\x00"
        + b"\x00"
    )
    return _frame(b"E", fields)
