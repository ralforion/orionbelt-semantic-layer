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

import datetime as _dt
import decimal
import logging
import re
import struct
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import sqlglot
import sqlglot.errors
from sqlglot import TokenType, exp
from sqlglot.dialects.postgres import Postgres

from orionbelt.pgwire import protocol

logger = logging.getLogger(__name__)


SQLSTATE_FEATURE_NOT_SUPPORTED = "0A000"
SQLSTATE_PROTOCOL_VIOLATION = "08P01"
SQLSTATE_INVALID_PARAM = "22023"


# Statement verbs we'll execute at Parse time (read-only / side-effect
# free). Everything else — DDL, DML, transaction control, SET, etc. —
# defers to Bind/Execute, even when it would otherwise qualify for the
# Describe('S')-needs-a-real-RowDescription preexec shortcut. The list
# is intentionally narrow; widening it requires a "this verb cannot
# mutate anything observable from another session" justification.
_RE_PREEXEC_SAFE_VERB = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*(select|show|values|with|table|explain)\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_preexec_safe(sql: str) -> bool:
    """True when ``sql`` starts with a verb we'll execute at Parse time.

    Skips leading SQL comments (line and block) before checking the
    verb, then rejects the read verbs that write (see below). Anything
    not matching — CREATE, DROP, INSERT, UPDATE,
    DELETE, ALTER, TRUNCATE, MERGE, GRANT, REVOKE, SET, RESET,
    BEGIN, COMMIT, ROLLBACK, SAVEPOINT, COPY, … — returns False and
    defers execution to Bind. Tableau's connect-check fires CREATE
    LOCAL TEMPORARY TABLE … via the extended protocol and a leftover
    table from a Parse-without-Bind cycle has caused real
    ``already exists`` regressions; skipping preexec for DDL is the
    fix.
    """

    if _RE_PREEXEC_SAFE_VERB.match(sql) is None or _RE_EXPLAIN_ANALYZE.match(sql):
        return False
    # A read verb can still write: ``SELECT ... INTO [TEMP] t``, a data-
    # modifying CTE (``WITH d AS (DELETE ...) SELECT ...``). The tokenizer
    # keeps quoted identifiers, strings and comments out of the check, so a
    # column called "Insert Date" is not a write. ``FOR UPDATE`` counts as one,
    # which only costs the early run.
    try:
        tokens = _TOKENIZER.tokenize(sql)
    except sqlglot.errors.SqlglotError:
        return False
    return not any(token.token_type in _WRITE_TOKENS for token in tokens)


_RE_EXPLAIN_ANALYZE = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*explain\s*(?:\([^)]*\banalyze\b|analyze\b)",
    re.IGNORECASE | re.DOTALL,
)
_TOKENIZER = Postgres.tokenizer_class()
_WRITE_TOKENS = frozenset(
    {
        TokenType.INTO,
        TokenType.INSERT,
        TokenType.UPDATE,
        TokenType.DELETE,
        TokenType.MERGE,
        TokenType.CREATE,
        TokenType.DROP,
        TokenType.ALTER,
        TokenType.TRUNCATE,
        TokenType.COPY,
        TokenType.GRANT,
    }
)


# Type alias matching :class:`PgWireServer`'s handler signature.
QueryHandler = Callable[..., Awaitable[bytes]]


@dataclass
class PreparedStatement:
    """A Parsed statement awaiting Bind.

    ``preexec_reply`` caches the result of running the statement at Parse
    time when the SQL has no parameter placeholders. The cached reply is
    used by ``Describe('S')`` so the JDBC driver gets a real
    ``RowDescription`` instead of ``NoData`` — without it, pgjdbc throws
    "Received resultset tuples, but no field structure for them" the
    moment ``Execute`` returns rows.
    """

    name: str
    sql: str
    param_oids: tuple[int, ...]
    preexec_reply: PortalReply | None = None
    # RowDescription for ``Describe('S')`` of a parameterised statement,
    # derived without the real values (see ``_describe_parameterised``).
    # Shape only: never replayed as a result.
    describe_reply: PortalReply | None = None
    # True once the client holds this statement's RowDescription, from
    # ``Describe('S')`` or from ``Describe('P')`` on one of its portals.
    # Later portals then skip the RowDescription at Execute: pgjdbc reuses
    # a named statement with Bind + Execute only, and a RowDescription it
    # did not ask for puts its reply queue out of step.
    row_description_sent: bool = False
    # The column type OIDs that RowDescription announced. A later Bind whose
    # result differs is refused (see ``_result_type_changed``): the client
    # would decode the new bytes by the old types.
    described_oids: tuple[int, ...] = ()


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
    described: bool = False  # Tracks whether Describe('P') was issued for this portal.
    # The client holds a description of the statement that this portal's
    # result no longer matches. A Describe('P') hands it the right one;
    # Execute without it is refused rather than misread.
    stale_description: bool = False


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

    async def parse(self, msg: protocol.ParseMessage) -> bytes:
        """Register a prepared statement, return ``ParseComplete``.

        Empty SQL is allowed — Postgres permits ``Parse("", "", [])``;
        a later ``Execute`` on the resulting portal returns
        ``EmptyQueryResponse``.

        For statements with no parameters we eagerly execute here so
        ``Describe('S')`` can return a real ``RowDescription`` — the
        JDBC driver locks in "no rows expected" the moment it sees
        ``NoData``, and any later ``DataRow`` then surfaces as
        "Received resultset tuples, but no field structure for them".
        """

        stmt = PreparedStatement(
            name=msg.statement_name,
            sql=msg.query,
            param_oids=msg.param_oids,
        )
        self._statements[msg.statement_name] = stmt
        await self._ensure_preexec(stmt)
        return protocol.build_parse_complete()

    async def _ensure_preexec(self, stmt: PreparedStatement) -> None:
        """Eagerly run the query at Parse time when it's safe to.

        Required so ``Describe('S')`` can return a real ``RowDescription``
        for canned probes (SHOW, SELECT current_*, …). Without this, JDBC
        is told ``NoData`` and then rejects the later ``DataRow`` frames
        with "Received resultset tuples, but no field structure for them".

        Strict Postgres semantics defer execution until Bind/Execute, so
        eager preexec is technically a protocol bend. We only do it for
        statements that are obviously side-effect free: SELECT, SHOW,
        VALUES, WITH/CTE. DDL and DML (CREATE, DROP, INSERT, UPDATE,
        etc.) skip preexec entirely — running them at Parse can mutate
        catalog or warehouse state before the client even gets to Bind,
        and Parse followed by Close (no Bind) would still have run them.
        """

        if stmt.preexec_reply is not None:
            return
        if stmt.param_oids and any(stmt.param_oids):
            # Parameterised — actual values arrive at Bind; we can't
            # pre-execute meaningfully here.
            return
        if "$" in stmt.sql:
            # Placeholder syntax even though param_oids is empty.
            return
        if not _is_preexec_safe(stmt.sql):
            return
        raw = await self._handler(stmt.sql, self._database)
        stmt.preexec_reply = _split_simple_reply(raw)

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

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "pgwire Bind portal=%r stmt=%r n_params=%d "
                "param_formats=%s result_formats=%s sql=%r",
                msg.portal_name,
                msg.statement_name,
                len(msg.param_values),
                list(msg.param_formats),
                list(msg.result_formats),
                # 2000 chars covers DBeaver's getColumns probe (~1.5kB
                # with all the JOINs to pg_attrdef / pg_depend) so
                # debugging "0 rows back" no longer hides the WHERE
                # clause behind a truncated tail.
                stmt.sql[:2000],
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

        # Reuse the Parse-time pre-execution when the SQL has no
        # parameters, the substituted SQL matches, AND the client
        # didn't request a non-default format code in Bind.
        # ``result_formats`` is empty (=all text) or ``(0,…)`` is the
        # server default we used in preexec; anything else means we
        # have to re-encode, which the simplest way means re-running.
        wants_default_formats = not msg.result_formats or all(f == 0 for f in msg.result_formats)
        if stmt.preexec_reply is not None and substituted == stmt.sql and wants_default_formats:
            reply = stmt.preexec_reply
        else:
            raw = await self._handler(
                substituted, self._database, result_formats=msg.result_formats
            )
            reply = _split_simple_reply(raw)
        portal = Portal(name=msg.portal_name, statement=stmt, reply=reply)
        portal.stale_description = stmt.row_description_sent and _result_type_changed(
            stmt, reply, msg.result_formats
        )
        # If Describe('S') already sent the RowDescription, the client
        # has the schema. Per the Postgres protocol Bind.result_formats
        # overrides the format_code in RowDescription per column —
        # pgjdbc applies that override when parsing DataRows. We do
        # NOT re-send RowDescription at Execute; sending it twice trips
        # pgjdbc's "Bad Connection" state.
        if stmt.row_description_sent:
            portal.described = True
        self._portals[msg.portal_name] = portal
        return protocol.build_bind_complete()

    # ------------------------------------------------------------------
    # Phase 3: Describe
    # ------------------------------------------------------------------

    async def describe(self, msg: protocol.DescribeMessage) -> bytes:
        if msg.target == b"S":
            stmt = self._statements.get(msg.name)
            if stmt is None:
                return protocol.build_error_response(
                    severity="ERROR",
                    code=SQLSTATE_PROTOCOL_VIOLATION,
                    message=f"prepared statement {msg.name!r} does not exist",
                )
            # Faithfully report the param count — falsely advertising
            # 1 TEXT parameter for a 0-param statement makes the JDBC
            # driver reject the later 0-value Bind.
            param_oids = list(stmt.param_oids) if stmt.param_oids else []
            # Use the pre-executed reply (if any) to give the JDBC driver
            # a real RowDescription. NoData would otherwise put pgjdbc
            # into "no rows" state and the later DataRow / RowDescription
            # at Execute trips "Received resultset tuples, but no field
            # structure for them".
            reply = stmt.preexec_reply or await self._describe_parameterised(stmt)
            if reply is not None:
                if reply.is_error:
                    return protocol.build_parameter_description(param_oids) + reply.error
                if reply.row_description:
                    _mark_described(stmt, reply.row_description)
                    return protocol.build_parameter_description(param_oids) + reply.row_description
            return protocol.build_parameter_description(param_oids) + protocol.build_no_data()

        portal = self._portals.get(msg.name)
        if portal is None:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_PROTOCOL_VIOLATION,
                message=f"portal {msg.name!r} does not exist",
            )
        portal.described = True
        portal.stale_description = False
        if portal.reply.is_error:
            return portal.reply.error
        if portal.reply.is_empty_query:
            return protocol.build_no_data()
        if not portal.reply.row_description:
            return protocol.build_no_data()
        _mark_described(portal.statement, portal.reply.row_description)
        return portal.reply.row_description

    async def _describe_parameterised(self, stmt: PreparedStatement) -> PortalReply | None:
        """RowDescription for a parameterised statement, before any Bind.

        Real Postgres knows a statement's result columns at Parse; here they
        come from running it. The columns never depend on the parameter
        values, so this runs the statement without them: first the SQL as
        written with every parameter a typed NULL (the catalog path accepts
        that),
        then, if that fails (OBSQL rejects ``= NULL``), a variant with WHERE,
        HAVING and OFFSET removed and ``LIMIT 0``. A statement that is not
        read-only, or for which neither works, keeps the ``NoData`` reply
        rather than an error: its Execute may still succeed.
        """

        if stmt.describe_reply is not None:
            return stmt.describe_reply
        if not _is_preexec_safe(stmt.sql):
            return None
        candidates = (
            _with_typed_nulls(stmt.sql, stmt.param_oids),
            # pgjdbc binds dates and timestamps unspecified; as text they do
            # not compare with a date, a bare NULL lets the context decide.
            _with_typed_nulls(stmt.sql, stmt.param_oids, unspecified="NULL"),
            _shape_only(stmt.sql, stmt.param_oids),
        )
        for sql in candidates:
            if sql is None:
                continue
            reply = _split_simple_reply(await self._handler(sql, self._database))
            if not reply.is_error:
                stmt.describe_reply = reply
                return reply
        return None

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
        if portal.stale_description:
            # Postgres refuses the same situation with this message.
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_FEATURE_NOT_SUPPORTED,
                message="cached plan must not change result type",
            )
        if portal.reply.is_empty_query:
            return protocol.build_empty_query_response()

        # When the client skipped Describe('P') (the JDBC fast path used
        # by Tableau and some other drivers does this), prepend the
        # cached RowDescription so the driver isn't surprised by a
        # DataRow without a preceding metadata frame ("Received resultset
        # tuples, but no field structure for them").
        body = b""
        if not portal.described and portal.reply.row_description:
            body += portal.reply.row_description
        body += b"".join(portal.reply.data_rows)
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


def _row_description_columns(row_description: bytes) -> list[tuple[int, int]]:
    """``(type OID, format code)`` per column of a RowDescription frame."""

    (count,) = struct.unpack("!H", row_description[5:7])
    columns: list[tuple[int, int]] = []
    pos = 7
    for _ in range(count):
        pos = row_description.index(b"\x00", pos) + 1
        _, _, oid, _, _, fmt = struct.unpack("!IhIhih", row_description[pos : pos + 18])
        columns.append((oid, fmt))
        pos += 18
    return columns


def _mark_described(stmt: PreparedStatement, row_description: bytes) -> None:
    """Record that the client now holds ``stmt``'s result columns."""

    stmt.row_description_sent = True
    stmt.described_oids = tuple(oid for oid, _ in _row_description_columns(row_description))


def _result_type_changed(
    stmt: PreparedStatement, reply: PortalReply, result_formats: tuple[int, ...]
) -> bool:
    """True when a Bind's result would be misread by a client that described it.

    That client decodes each column by the type it was told and the format it
    asked for in Bind, and gets no new RowDescription. A different type (the
    describe-time placeholder bound differently), or a column the server can
    only send as text after binary was asked for, would be decoded wrongly
    without an error.
    """

    if reply.is_error or not reply.row_description:
        return False
    columns = _row_description_columns(reply.row_description)
    if tuple(oid for oid, _ in columns) != stmt.described_oids:
        return True
    if not result_formats:
        requested = [0] * len(columns)
    elif len(result_formats) == 1:
        requested = [result_formats[0]] * len(columns)
    else:
        requested = list(result_formats)
    return [fmt for _, fmt in columns] != requested


def _with_typed_nulls(
    sql: str, param_oids: tuple[int, ...], unspecified: str = "CAST(NULL AS VARCHAR)"
) -> str | None:
    """``sql`` with every ``$N`` bound to a NULL of the type Bind would give it.

    An unspecified parameter (OID 0) takes ``unspecified``: text by default,
    the type Postgres gives an unknown in a select list, or a bare NULL where
    the context must decide (``DATE '2024-01-02' > $1``).

    The scanner finds the placeholders, so a ``$N`` inside a literal or a
    comment is not one, and nothing is allocated per index: ``$1000000`` in a
    string costs nothing. An index past Postgres's own parameter limit, or one
    the statement never declared, leaves the statement undescribed (None).
    """

    def render(idx: int) -> str:
        if idx >= _MAX_PARAMETERS:
            raise _BadParameterError(f"Placeholder ${idx + 1} exceeds the parameter limit")
        oid = param_oids[idx] if idx < len(param_oids) else 0
        return unspecified if oid == _OID_UNSPECIFIED else _typed("NULL", oid)

    try:
        return _map_placeholders(sql, render)
    except _BadParameterError:
        return None


#: Postgres caps a statement at 65535 parameters (an int16 count on the wire).
_MAX_PARAMETERS = 65535


def _shape_only(sql: str, param_oids: tuple[int, ...]) -> str | None:
    """A SELECT reduced to its result shape: no WHERE / HAVING / OFFSET, LIMIT 0.

    Placeholders left elsewhere (a JOIN condition, the select list) are bound
    to typed NULLs. None when ``sql`` is not a single SELECT sqlglot can
    round-trip.
    """

    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(tree, exp.Select):
        return None
    for clause in ("where", "having", "offset"):
        tree.set(clause, None)
    return _with_typed_nulls(tree.limit(0).sql(dialect="postgres"), param_oids)


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

    Supports text (format=0) for any OID, plus a small set of
    binary-format OIDs the JDBC connect-check actually emits — INT2,
    INT4, INT8, BOOL, FLOAT4, FLOAT8, TEXT/VARCHAR/NAME, BYTEA. Binary
    parameters whose OID we don't recognise raise
    :class:`_BinaryParameterError` so the caller can surface
    ``feature_not_supported``.

    Quoting follows the Postgres standard-conforming-strings rule: wrap
    string values in single quotes and double any embedded single
    quote. Numerics / booleans render unquoted. Each value, NULL included,
    is then cast to the type its OID binds as (``_PARAM_SQL_TYPE``), so the
    result type does not depend on the value.
    """

    rendered: list[str] = []
    for idx, (raw, fmt) in enumerate(zip(values, formats, strict=True)):
        oid = param_oids[idx] if idx < len(param_oids) else 0
        rendered.append(_typed(_render_literal(raw, fmt, oid), oid))
    return _replace_placeholders(sql, rendered)


def _render_literal(raw: bytes | None, fmt: int, oid: int) -> str:
    """One parameter value as a SQL literal, before :func:`_typed` casts it."""

    if raw is None:
        return "NULL"
    if fmt == 1:
        return _decode_binary_param(raw, oid)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BadParameterError(f"Text-format parameter is not valid UTF-8: {exc}") from None
    if oid in _NUMERIC_TEXT_OIDS:
        # Never splice client bytes into SQL raw: strictly parse the text
        # per its declared numeric OID and re-render a canonical literal.
        # A value like ``0 AND "x" = 'y'`` fails the parse and is rejected
        # instead of becoming active SQL.
        canonical = _canonical_numeric_text(text, oid)
        if oid == _OID_FLOAT4:
            # The value a real holds: ``0.1`` is 0.10000000149011612, which is
            # what Postgres compares against for ``$1::real``. Rendering it
            # exactly also makes ``CAST(... AS REAL)`` provably lossless.
            try:
                (as_real,) = struct.unpack("!f", struct.pack("!f", float(canonical)))
            except OverflowError:
                raise _BadParameterError(f"float4 parameter out of range: {text!r}") from None
            return repr(as_real)
        return canonical
    if oid == _OID_UUID:
        # Canonical form, as ``CAST(... AS UUID)`` would produce; an invalid
        # value is refused, as Postgres refuses it.
        try:
            return f"'{uuid.UUID(text)}'"
        except ValueError:
            raise _BadParameterError(f"Invalid uuid parameter: {text!r}") from None
    if oid in _BOOL_TEXT_OIDS:
        return "TRUE" if text.lower() in {"t", "true", "1", "y", "yes"} else "FALSE"
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


# Postgres binary parameter OIDs we know how to decode. Anything else
# in binary format trips _BinaryParameterError so we surface a clean
# error rather than mangling unknown bytes.
_OID_BOOL = 16
_OID_BYTEA = 17
_OID_INT8 = 20
_OID_INT2 = 21
_OID_INT4 = 23
_OID_TEXT = 25
_OID_FLOAT4 = 700
_OID_FLOAT8 = 701
_OID_VARCHAR = 1043
_OID_NAME = 19
_OID_BPCHAR = 1042

_OID_NUMERIC = 1700
_OID_UUID = 2950

_INTEGER_TEXT_OIDS: frozenset[int] = frozenset({_OID_INT2, _OID_INT4, _OID_INT8})
_INT_TEXT_RE = re.compile(r"[+-]?[0-9]+")
# ASCII decimal/float literal (sign, digits, optional fraction, optional
# exponent). Postgres numeric text input has no room for underscores or
# unicode digits, which ``Decimal`` would otherwise accept and silently
# normalize; match the DB grammar so those forms are rejected.
_NUMERIC_TEXT_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NUMERIC_TEXT_OIDS: frozenset[int] = frozenset(
    {_OID_INT2, _OID_INT4, _OID_INT8, _OID_FLOAT4, _OID_FLOAT8, _OID_NUMERIC}
)
_BOOL_TEXT_OIDS: frozenset[int] = frozenset({_OID_BOOL})

#: The SQL type each parameter binds as, by its declared OID; other declared
#: types (text, varchar, name, ...) bind as text, as their quoted literal
#: always did. Bind and Describe both render a parameter as
#: ``CAST(<value> AS <type>)``, a NULL included, so the result type a
#: describe-time run reports is the one Bind produces whatever the value:
#: ``SELECT $1`` bound to ``1`` must not turn into the canned ``SELECT 1``.
#: Temporal OIDs keep their type, so ``DATE '2024-01-02' > $1`` still
#: compares dates. ``numeric`` is sized from the value (``_numeric_type``).
_PARAM_SQL_TYPE: dict[int, str] = {
    _OID_INT2: "SMALLINT",
    _OID_INT4: "INTEGER",
    _OID_INT8: "BIGINT",
    _OID_FLOAT4: "REAL",
    _OID_FLOAT8: "DOUBLE",
    _OID_BOOL: "BOOLEAN",
    _OID_BYTEA: "BLOB",
    1082: "DATE",
    1083: "TIME",
    1114: "TIMESTAMP",
    1184: "TIMESTAMPTZ",
    1186: "INTERVAL",
    1266: "TIMETZ",
    _OID_UUID: "UUID",
}

#: OID 0: the client left the type to the server. Postgres infers it from
#: context, which an explicit cast would take away (``date > $1`` with a
#: date string, or a NULL), so Bind leaves the literal bare. Describe has no
#: value to bind and uses text, the type Postgres gives an unknown in a
#: select list.
_OID_UNSPECIFIED = 0

#: DuckDB's widest exact DECIMAL.
_MAX_DECIMAL_PRECISION = 38


def _typed(literal: str, oid: int) -> str:
    """``literal`` cast to the SQL type a parameter of ``oid`` binds as."""

    if oid == _OID_UNSPECIFIED:
        return literal
    if oid == _OID_NUMERIC:
        sql_type = _numeric_type(literal)
        return literal if sql_type is None else f"CAST({literal} AS {sql_type})"
    return f"CAST({literal} AS {_PARAM_SQL_TYPE.get(oid, 'VARCHAR')})"


def _numeric_type(literal: str) -> str | None:
    """The exact ``DECIMAL(p, s)`` for a numeric parameter, None past 38 digits.

    A fixed type would round (DuckDB's bare DECIMAL is (18, 3)) or, as DOUBLE,
    lose digits: ``9007199254740993.00`` must stay that value. A NULL takes the
    widest type; its OID, NUMERIC, is what Describe and Bind must agree on.
    """

    if literal == "NULL":
        return f"DECIMAL({_MAX_DECIMAL_PRECISION}, 10)"
    value = decimal.Decimal(literal)
    exponent = value.as_tuple().exponent
    scale = max(0, -exponent) if isinstance(exponent, int) else 0
    precision = max(1, value.adjusted() + 1) + scale
    if precision > _MAX_DECIMAL_PRECISION:
        return None
    return f"DECIMAL({precision}, {scale})"


def _canonical_numeric_text(text: str, oid: int) -> str:
    """Parse a text-format numeric parameter and re-render it canonically.

    Security boundary for the extended-protocol Bind path: a numeric-typed
    parameter's *text* is attacker-controlled, so it must never reach the SQL
    string unvalidated. We parse it strictly per the declared OID and emit the
    canonical string form of the parsed number — which contains only
    ``[-+0-9.eE]`` and can carry no SQL syntax. Anything that is not a plain
    number (``0 AND 1=1``, ``1); DROP TABLE t; --``, ``NaN``, ``Infinity``) is
    rejected with :class:`_BadParameterError`.
    """
    s = text.strip()
    try:
        if oid in _INTEGER_TEXT_OIDS:
            # Strict ASCII integer grammar (Python int() also accepts
            # underscores / unicode digits — harmless to render but not what
            # Postgres accepts, so reject for predictability).
            if not _INT_TEXT_RE.fullmatch(s):
                raise ValueError("not a plain integer")
            return str(int(s))
        # Float / numeric: enforce the ASCII decimal grammar before Decimal so
        # underscores and unicode digits are rejected rather than normalized.
        if not _NUMERIC_TEXT_RE.fullmatch(s):
            raise ValueError("not a plain decimal/float")
        value = decimal.Decimal(s)
    except (ValueError, ArithmeticError) as exc:
        raise _BadParameterError(f"Invalid numeric text parameter for OID {oid}: {text!r}") from exc
    if not value.is_finite():
        raise _BadParameterError(
            f"Non-finite numeric text parameter not allowed for OID {oid}: {text!r}"
        )
    return str(value)


def _decode_binary_param(raw: bytes, oid: int) -> str:
    """Decode a Postgres binary-format value into a SQL literal.

    The wire formats here match PostgreSQL's ``send`` functions: all
    multi-byte integers / floats are network byte order (big-endian).
    """

    if oid in {_OID_INT2}:
        if len(raw) != 2:
            raise _BadParameterError(f"INT2 binary param must be 2 bytes, got {len(raw)}")
        return str(struct.unpack("!h", raw)[0])
    if oid == _OID_INT4:
        if len(raw) != 4:
            raise _BadParameterError(f"INT4 binary param must be 4 bytes, got {len(raw)}")
        return str(struct.unpack("!i", raw)[0])
    if oid == _OID_INT8:
        if len(raw) != 8:
            raise _BadParameterError(f"INT8 binary param must be 8 bytes, got {len(raw)}")
        return str(struct.unpack("!q", raw)[0])
    if oid == _OID_BOOL:
        if len(raw) != 1:
            raise _BadParameterError(f"BOOL binary param must be 1 byte, got {len(raw)}")
        return "TRUE" if raw[0] else "FALSE"
    if oid == _OID_FLOAT4:
        if len(raw) != 4:
            raise _BadParameterError(f"FLOAT4 binary param must be 4 bytes, got {len(raw)}")
        return repr(struct.unpack("!f", raw)[0])
    if oid == _OID_FLOAT8:
        if len(raw) != 8:
            raise _BadParameterError(f"FLOAT8 binary param must be 8 bytes, got {len(raw)}")
        return repr(struct.unpack("!d", raw)[0])
    if oid in {_OID_TEXT, _OID_VARCHAR, _OID_NAME, _OID_BPCHAR}:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _BadParameterError(
                f"Binary text-typed parameter is not valid UTF-8: {exc}"
            ) from None
        return "'" + text.replace("'", "''") + "'"
    if oid == _OID_BYTEA:
        return "'\\x" + raw.hex() + "'::bytea"
    decode = _TEMPORAL_DECODERS.get(oid)
    if decode is not None:
        return decode(raw)
    if oid == _OID_NUMERIC:
        return _decode_binary_numeric(raw)
    if oid == _OID_UUID:
        if len(raw) != 16:
            raise _BadParameterError(f"UUID binary param must be 16 bytes, got {len(raw)}")
        return f"'{uuid.UUID(bytes=raw)}'"
    raise _BinaryParameterError(
        f"Binary-format parameter for OID {oid} not supported (supported: INT2/INT4/INT8/"
        "BOOL/FLOAT4/FLOAT8/NUMERIC/TEXT/VARCHAR/NAME/BPCHAR/BYTEA/DATE/TIME/TIMESTAMP/"
        "TIMESTAMPTZ/INTERVAL/UUID)"
    )


# Postgres's binary date / time formats count from its own epoch, 2000-01-01,
# in days (date) or microseconds (time, timestamp); a timestamptz is UTC.
# psycopg 3 binds dates, datetimes and timedeltas in this format by default.
_PG_EPOCH_DATE = _dt.date(2000, 1, 1)
_PG_EPOCH = _dt.datetime(2000, 1, 1)
# Postgres's -infinity / infinity: the int32 extremes for a date, the int64
# extremes for a timestamp (int32 extremes are ordinary timestamps near 2000).
_INFINITE_DATE = {2**31 - 1, -(2**31)}
_INFINITE_TIMESTAMP = {2**63 - 1, -(2**63)}
#: Widest NUMERIC parameter accepted, in decimal digits either side of the
#: point; the OBSQL translator refuses wider literals as well.
_MAX_NUMERIC_DIGITS = 1000


def _fixed(raw: bytes, size: int, name: str) -> None:
    if len(raw) != size:
        raise _BadParameterError(f"{name} binary param must be {size} bytes, got {len(raw)}")


def _finite(value: int, name: str, sentinels: set[int]) -> int:
    if value in sentinels:
        raise _BadParameterError(f"Infinite {name} parameter not supported")
    return value


def _decode_binary_date(raw: bytes) -> str:
    _fixed(raw, 4, "DATE")
    (days,) = struct.unpack("!i", raw)
    try:
        value = _PG_EPOCH_DATE + _dt.timedelta(days=_finite(days, "date", _INFINITE_DATE))
    except OverflowError:
        raise _BadParameterError("DATE parameter out of range") from None
    return f"'{value.isoformat()}'"


def _timestamp(raw: bytes, name: str) -> _dt.datetime:
    _fixed(raw, 8, name)
    (micros,) = struct.unpack("!q", raw)
    try:
        return _PG_EPOCH + _dt.timedelta(
            microseconds=_finite(micros, name.lower(), _INFINITE_TIMESTAMP)
        )
    except OverflowError:
        raise _BadParameterError(f"{name} parameter out of range") from None


def _decode_binary_timestamp(raw: bytes) -> str:
    return f"'{_timestamp(raw, 'TIMESTAMP').isoformat(sep=' ')}'"


def _decode_binary_timestamptz(raw: bytes) -> str:
    # The wire value is UTC; spell the offset so the instant is unambiguous.
    instant = _timestamp(raw, "TIMESTAMPTZ").replace(tzinfo=_dt.UTC)
    return f"'{instant.isoformat(sep=' ')}'"


def _decode_binary_time(raw: bytes) -> str:
    _fixed(raw, 8, "TIME")
    (micros,) = struct.unpack("!q", raw)
    if not 0 <= micros <= 86_400_000_000:
        raise _BadParameterError("TIME parameter out of range")
    if micros == 86_400_000_000:
        return "'24:00:00'"
    seconds, micro = divmod(micros, 1_000_000)
    return f"'{_dt.time(seconds // 3600, seconds // 60 % 60, seconds % 60, micro).isoformat()}'"


def _decode_binary_interval(raw: bytes) -> str:
    _fixed(raw, 16, "INTERVAL")
    micros, days, months = struct.unpack("!qii", raw)
    return f"'{months} months {days} days {micros} microseconds'"


_TEMPORAL_DECODERS: dict[int, Callable[[bytes], str]] = {
    1082: _decode_binary_date,
    1083: _decode_binary_time,
    1114: _decode_binary_timestamp,
    1184: _decode_binary_timestamptz,
    1186: _decode_binary_interval,
}

_NUMERIC_NEG = 0x4000
_NUMERIC_POS = 0x0000


def _decode_binary_numeric(raw: bytes) -> str:
    """Postgres NUMERIC binary (``numeric_send``) as a plain decimal literal.

    ``ndigits, weight, sign, dscale`` then ``ndigits`` base-10000 digits; the
    first digit is worth 10000 ** weight. NaN and infinities are refused, as
    the text path refuses them.
    """

    if len(raw) < 8:
        raise _BadParameterError("NUMERIC binary param is shorter than its header")
    ndigits, weight, sign, dscale = struct.unpack("!hhHH", raw[:8])
    if sign not in (_NUMERIC_POS, _NUMERIC_NEG):
        raise _BadParameterError("Non-finite NUMERIC parameter not allowed")
    if ndigits < 0 or len(raw) != 8 + 2 * ndigits:
        raise _BadParameterError("NUMERIC binary param has a malformed digit count")
    if 4 * abs(weight) > _MAX_NUMERIC_DIGITS or dscale > _MAX_NUMERIC_DIGITS:
        raise _BadParameterError("NUMERIC parameter out of range")
    digits = struct.unpack(f"!{ndigits}H", raw[8:])
    if any(digit > 9999 for digit in digits):
        raise _BadParameterError("NUMERIC binary param has an invalid digit")
    # Built from its digit tuple, which is exact: decimal arithmetic would
    # round to the context's 28 significant digits.
    figures = [int(ch) for ch in "".join(f"{digit:04d}" for digit in digits)] or [0]
    exponent = 4 * (weight - ndigits + 1) if digits else 0
    if exponent > -dscale:  # pad to the display scale
        figures += [0] * (exponent + dscale)
        exponent = -dscale
    while exponent < -dscale and figures[-1] == 0 and len(figures) > 1:
        figures.pop()  # trailing zero groups below the display scale
        exponent += 1
    value = decimal.Decimal((1 if sign == _NUMERIC_NEG else 0, tuple(figures), exponent))
    return format(value, "f")


# Opener for a dollar-quoted string: ``$$`` or ``$tag$`` where ``tag`` is a
# SQL identifier (letter/underscore start). A digit-led ``$1`` is NOT a valid
# dollar-quote tag, so it stays unambiguously a bind placeholder.
_DOLLAR_QUOTE_OPEN = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _replace_placeholders(sql: str, rendered: list[str]) -> str:
    """Replace ``$N`` bind placeholders with their rendered literal."""

    def render(idx: int) -> str:
        if idx >= len(rendered):
            raise _BadParameterError(f"Placeholder ${idx + 1} has no bound value")
        return rendered[idx]

    return _map_placeholders(sql, render)


def _map_placeholders(sql: str, render: Callable[[int], str]) -> str:
    """Replace each ``$N`` bind placeholder with ``render(N - 1)``.

    A small SQL-aware scanner substitutes ``$N`` **only** in default context.
    It skips over string literals (``'…''…'``), quoted identifiers (``"…""…"``),
    line comments (``-- … <newline>``), block comments (``/* … */``, nested),
    and dollar-quoted strings (``$tag$ … $tag$``). Without this a ``$N`` inside
    a comment or dollar-quote would be substituted, letting a text parameter
    that contains a newline, ``*/``, or ``$$`` break out of that context and
    inject SQL. Only bare ``$digits`` in default context is a placeholder.
    """

    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # Line comment: -- … up to and including the newline (or EOL).
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i + 2)
            end = n if nl == -1 else nl + 1
            out.append(sql[i:end])
            i = end
            continue

        # Block comment: /* … */, nesting per Postgres.
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if sql[j] == "/" and j + 1 < n and sql[j + 1] == "*":
                    depth += 1
                    j += 2
                elif sql[j] == "*" and j + 1 < n and sql[j + 1] == "/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(sql[i:j])
            i = j
            continue

        # Single-quoted string literal ('' escapes an embedded quote).
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        # Double-quoted identifier ("" escapes an embedded quote).
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        if ch == "$":
            # Dollar-quoted string: copy verbatim through the matching close
            # tag (never substitute inside it).
            m = _DOLLAR_QUOTE_OPEN.match(sql, i)
            if m:
                tag = m.group(0)
                close = sql.find(tag, m.end())
                end = n if close == -1 else close + len(tag)
                out.append(sql[i:end])
                i = end
                continue
            # Bind placeholder $N (digits only; disjoint from dollar-quote tags).
            if i + 1 < n and sql[i + 1].isdigit():
                j = i + 1
                while j < n and sql[j].isdigit():
                    j += 1
                idx = int(sql[i + 1 : j]) - 1
                if idx < 0:
                    raise _BadParameterError(f"Placeholder ${idx + 1} has no bound value")
                out.append(render(idx))
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
