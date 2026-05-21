"""Extended Postgres query protocol — Parse / Bind / Describe / Execute / Sync.

The simple-query path (Step 1–3) compiles the whole reply on one round-
trip; extended-query carries five separate phases that JDBC drivers,
psycopg in prepared-statement mode, and most BI tools rely on. This
module owns the per-connection state machine plus the parameter
substitution that lets us reuse the existing :class:`SemanticRouter`
pipeline unchanged.

Step 4 implementation choices, documented up-front because they change
the trade space we revisit in Step 7:

* **Eager bind.** When ``Bind`` arrives we substitute the parameter
  values into the SQL string and run the full router pipeline once.
  The reply bytes are split into row_description / data_rows /
  command_complete (or a single ErrorResponse) and cached on the
  portal. ``Describe('P')`` and ``Execute`` then replay slices from
  that cache. This trades prepared-statement reuse for a much smaller
  implementation than full parameter-aware compilation. It matches the
  pragmatic path discussed in design/PLAN_postgres_wire.md §8.

* **Text format only.** Parameter values arrive in either text (format
  code 0) or binary (1). Step 7 owns binary; until then, binary params
  surface as a clean ErrorResponse with SQLSTATE 0A000.

* **Statement / portal name discipline.** The unnamed statement and
  portal (`""`) are overwritten by each new Parse / Bind, matching
  Postgres semantics. Named statements / portals persist until the
  client sends ``Close`` (or the connection ends).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from orionbelt.pgwire import protocol

logger = logging.getLogger(__name__)


SQLSTATE_FEATURE_NOT_SUPPORTED = "0A000"
SQLSTATE_PROTOCOL_VIOLATION = "08P01"
SQLSTATE_INVALID_PARAM = "22023"


# Type alias matching :class:`PgWireServer`'s handler signature.
QueryHandler = Callable[[str, str], Awaitable[bytes]]


@dataclass
class PreparedStatement:
    """A Parsed statement awaiting Bind."""

    name: str
    sql: str
    param_oids: tuple[int, ...]


@dataclass
class PortalReply:
    """Cached wire bytes for one Bind, split for replay.

    Either ``error`` is populated (and the other fields are empty) or
    the row_description / data_rows / command_complete tuple is.
    """

    row_description: bytes = b""
    data_rows: tuple[bytes, ...] = ()
    command_complete: bytes = b""
    error: bytes = b""

    @property
    def is_error(self) -> bool:
        return bool(self.error)

    @property
    def is_empty_query(self) -> bool:
        """True when the prepared statement was whitespace only.

        The router signals an empty query with ``CommandComplete("")``
        — i.e. a ``C`` frame whose body is the single NUL terminator
        of an empty command tag.  We detect that frame here so the
        Execute reply can promote it to ``EmptyQueryResponse``.
        """

        return (
            not self.row_description
            and not self.error
            and self.command_complete == b"C\x00\x00\x00\x05\x00"
        )


@dataclass
class Portal:
    """A Bound portal cached for the Describe / Execute round trip."""

    name: str
    statement: PreparedStatement
    reply: PortalReply = field(default_factory=PortalReply)


class ExtendedSession:
    """Per-connection state for the extended query protocol."""

    def __init__(self, *, handler: QueryHandler, database: str) -> None:
        self._handler = handler
        self._database = database
        self._statements: dict[str, PreparedStatement] = {}
        self._portals: dict[str, Portal] = {}

    # ------------------------------------------------------------------
    # Phase 1: Parse
    # ------------------------------------------------------------------

    def parse(self, msg: protocol.ParseMessage) -> bytes:
        """Register a prepared statement, return ``ParseComplete``.

        Empty SQL is allowed — Postgres permits ``Parse("", "", [])``;
        a later ``Execute`` on the resulting portal returns
        ``EmptyQueryResponse``.
        """

        self._statements[msg.statement_name] = PreparedStatement(
            name=msg.statement_name,
            sql=msg.query,
            param_oids=msg.param_oids,
        )
        return protocol.build_parse_complete()

    # ------------------------------------------------------------------
    # Phase 2: Bind  (eagerly runs the query — see module docstring)
    # ------------------------------------------------------------------

    async def bind(self, msg: protocol.BindMessage) -> bytes:
        stmt = self._statements.get(msg.statement_name)
        if stmt is None:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_PROTOCOL_VIOLATION,
                message=f"prepared statement {msg.statement_name!r} does not exist",
            )

        formats = _expand_param_formats(msg.param_formats, len(msg.param_values))
        try:
            substituted = substitute_parameters(
                stmt.sql, msg.param_values, formats, stmt.param_oids
            )
        except _BinaryParameterError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_FEATURE_NOT_SUPPORTED,
                message=str(exc),
            )
        except _BadParameterError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_INVALID_PARAM,
                message=str(exc),
            )

        raw = await self._handler(substituted, self._database)
        reply = _split_simple_reply(raw)
        self._portals[msg.portal_name] = Portal(name=msg.portal_name, statement=stmt, reply=reply)
        return protocol.build_bind_complete()

    # ------------------------------------------------------------------
    # Phase 3: Describe
    # ------------------------------------------------------------------

    def describe(self, msg: protocol.DescribeMessage) -> bytes:
        if msg.target == b"S":
            stmt = self._statements.get(msg.name)
            if stmt is None:
                return protocol.build_error_response(
                    severity="ERROR",
                    code=SQLSTATE_PROTOCOL_VIOLATION,
                    message=f"prepared statement {msg.name!r} does not exist",
                )
            # We don't try to infer column types pre-Bind; the standard
            # NoData reply tells the client to call Describe('P') after
            # Bind once the row shape is known.
            param_oids = list(stmt.param_oids) if stmt.param_oids else [protocol.OID_TEXT]
            return protocol.build_parameter_description(param_oids) + protocol.build_no_data()

        portal = self._portals.get(msg.name)
        if portal is None:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_PROTOCOL_VIOLATION,
                message=f"portal {msg.name!r} does not exist",
            )
        if portal.reply.is_error:
            return portal.reply.error
        if portal.reply.is_empty_query:
            return protocol.build_no_data()
        if not portal.reply.row_description:
            return protocol.build_no_data()
        return portal.reply.row_description

    # ------------------------------------------------------------------
    # Phase 4: Execute
    # ------------------------------------------------------------------

    def execute(self, msg: protocol.ExecuteMessage) -> bytes:
        portal = self._portals.get(msg.portal_name)
        if portal is None:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_PROTOCOL_VIOLATION,
                message=f"portal {msg.portal_name!r} does not exist",
            )
        if portal.reply.is_error:
            return portal.reply.error
        if portal.reply.is_empty_query:
            return protocol.build_empty_query_response()

        body = b"".join(portal.reply.data_rows)
        body += portal.reply.command_complete
        return body

    # ------------------------------------------------------------------
    # Phase 5: Close
    # ------------------------------------------------------------------

    def close(self, msg: protocol.CloseMessage) -> bytes:
        if msg.target == b"S":
            self._statements.pop(msg.name, None)
        else:
            self._portals.pop(msg.name, None)
        return protocol.build_close_complete()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _BinaryParameterError(Exception):
    """Raised when the client supplies a binary-format parameter."""


class _BadParameterError(Exception):
    """Raised when a text parameter can't be decoded."""


def _expand_param_formats(formats: tuple[int, ...], n_params: int) -> list[int]:
    """Spread the Bind ``param_formats`` over each parameter.

    Per Postgres: empty list ⇒ all text, length 1 ⇒ apply to every
    parameter, length N ⇒ one per parameter (must match ``n_params``).
    """

    if not formats:
        return [0] * n_params
    if len(formats) == 1:
        return [formats[0]] * n_params
    if len(formats) != n_params:
        raise _BadParameterError(
            f"Bind format-code count {len(formats)} doesn't match parameter count {n_params}"
        )
    return list(formats)


def substitute_parameters(
    sql: str,
    values: tuple[bytes | None, ...],
    formats: list[int],
    param_oids: tuple[int, ...] = (),
) -> str:
    """Inline ``$1`` / ``$2`` … placeholders with safely-quoted values.

    Text format (code 0) is the original Step 4 path: UTF-8 decode and
    wrap in single quotes.  Binary format (code 1) is decoded per the
    declared parameter OID — int / float / bool / timestamp / date /
    text are all covered.  Unrecognised OIDs fall back to a UTF-8
    decode, which usually works for ``text``-typed parameters whose
    OID the client omitted.  Truly opaque binary values raise
    :class:`_BinaryParameterError`.

    Quoting follows the Postgres standard-conforming-strings rule: wrap
    the value in single quotes and double any embedded single quote.
    Backslashes are left alone because the server advertises
    ``standard_conforming_strings = on`` in ParameterStatus.
    """

    rendered: list[str] = []
    for idx, (raw, fmt) in enumerate(zip(values, formats, strict=True)):
        if raw is None:
            rendered.append("NULL")
            continue
        oid = param_oids[idx] if idx < len(param_oids) else 0
        if fmt == 1:
            text = _decode_binary_param(raw, oid)
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _BadParameterError(
                    f"Text-format parameter is not valid UTF-8: {exc}"
                ) from None
        escaped = text.replace("'", "''")
        rendered.append(f"'{escaped}'")
    return _replace_placeholders(sql, rendered)


def _decode_binary_param(raw: bytes, oid: int) -> str:
    """Render a binary-format Bind parameter as its text-form literal.

    DuckDB tolerates ``WHERE int_col = '42'`` (implicit cast), so we
    always return a string and let the existing quoting path wrap it
    in single quotes — same shape as the text-format path. Worst case
    we lose type precision, but BI tools (DBeaver / Tableau / JDBC)
    overwhelmingly bind oids and short strings where this is fine.
    """

    if oid == 16:  # bool
        return "t" if raw == b"\x01" else "f"
    if oid == 21 and len(raw) == 2:  # int2
        return str(struct.unpack("!h", raw)[0])
    if oid == 23 and len(raw) == 4:  # int4
        return str(struct.unpack("!i", raw)[0])
    if oid == 20 and len(raw) == 8:  # int8
        return str(struct.unpack("!q", raw)[0])
    if oid == 700 and len(raw) == 4:  # float4
        return str(struct.unpack("!f", raw)[0])
    if oid == 701 and len(raw) == 8:  # float8
        return str(struct.unpack("!d", raw)[0])
    if oid in (25, 1043, 1042, 19):  # text / varchar / bpchar / name
        return raw.decode("utf-8", errors="replace")
    if oid == 0:
        # OID 0 = "unspecified" — most BI tools omit oids for textual
        # params and assume the server can sniff. Decoding as UTF-8 is
        # the safe default.
        return raw.decode("utf-8", errors="replace")
    if oid == 1082 and len(raw) == 4:  # date — days since 2000-01-01
        from datetime import date, timedelta

        days = struct.unpack("!i", raw)[0]
        return (date(2000, 1, 1) + timedelta(days=days)).isoformat()
    if oid == 1114 and len(raw) == 8:  # timestamp — µs since 2000-01-01
        from datetime import datetime, timedelta

        microseconds = struct.unpack("!q", raw)[0]
        return (datetime(2000, 1, 1) + timedelta(microseconds=microseconds)).isoformat(sep=" ")
    if oid == 1184 and len(raw) == 8:  # timestamptz — µs since 2000-01-01 UTC
        from datetime import UTC, datetime, timedelta

        microseconds = struct.unpack("!q", raw)[0]
        ts = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
        return ts.isoformat(sep=" ")
    # Unknown OID with binary bytes — fall back to UTF-8 decode rather
    # than failing the whole connection.  If the bytes aren't text,
    # DuckDB will reject the resulting comparison cleanly.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BinaryParameterError(
            f"Cannot decode binary parameter (oid={oid}, {len(raw)} bytes): {exc}"
        ) from None


def _replace_placeholders(sql: str, rendered: list[str]) -> str:
    """Replace ``$N`` placeholders with their rendered literal.

    Implements a tiny hand-rolled scan so we don't trample on dollar-
    quoted strings (``$tag$ … $tag$``) or numbers occurring inside
    string literals.
    """

    out: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    in_double = False
    while i < n:
        ch = sql[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                # Doubled single quote stays inside the literal.
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            out.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "'":
            out.append(ch)
            in_single = True
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_double = True
            i += 1
            continue
        if ch == "$" and i + 1 < n and sql[i + 1].isdigit():
            # Read the placeholder index.
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            idx = int(sql[i + 1 : j]) - 1
            if idx < 0 or idx >= len(rendered):
                raise _BadParameterError(f"Placeholder ${idx + 1} has no bound value")
            out.append(rendered[idx])
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_simple_reply(raw: bytes) -> PortalReply:
    """Split a router reply into its constituent frames.

    The router's :meth:`SemanticRouter.handle` returns either:

    * RowDescription (``T``) + DataRow* (``D``) + CommandComplete (``C``)
    * a single ErrorResponse (``E``)
    * a single CommandComplete with empty tag (whitespace-only query)

    We walk the bytes once and pull each frame out so the portal can
    replay individual phases later.
    """

    reply = PortalReply()
    offset = 0
    n = len(raw)
    data_rows: list[bytes] = []
    while offset < n:
        tag = raw[offset : offset + 1]
        if offset + 5 > n:
            raise protocol.ProtocolError("Truncated frame in router reply")
        (length,) = struct.unpack("!I", raw[offset + 1 : offset + 5])
        end = offset + 1 + length
        if end > n:
            raise protocol.ProtocolError("Frame length exceeds router reply size")
        frame = raw[offset:end]
        if tag == b"T":
            reply.row_description = frame
        elif tag == b"D":
            data_rows.append(frame)
        elif tag == b"C":
            reply.command_complete = frame
        elif tag == b"E":
            reply.error = frame
        else:
            logger.debug("pgwire extended: unrecognised tag %r in cached reply", tag)
        offset = end
    reply.data_rows = tuple(data_rows)
    return reply
