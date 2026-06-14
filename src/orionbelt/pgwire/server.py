"""Asyncio Postgres wire server — Step 1.

One task per connection; the lifecycle is:

1. (optional) SSLRequest → reply ``N`` and continue plaintext.
2. StartupMessage → authenticate → AuthenticationOk + ParameterStatus
   + BackendKeyData + ReadyForQuery.
3. Loop on Simple Query (``Q``) frames; reply with a canned answer for
   ``SELECT 1`` and an ErrorResponse for everything else. The router
   replaces this hardcoded behaviour in Step 2.
4. Terminate (``X``) or EOF closes the connection.

Connection cap is enforced with an ``asyncio.Semaphore``; per-query wall
clock with ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import struct
from collections.abc import Awaitable, Callable

from orionbelt.pgwire import protocol
from orionbelt.pgwire.auth import (
    MECH_CLEARTEXT,
    auth_required,
    authenticate,
    scram_candidate_keys,
    select_mechanism,
)
from orionbelt.pgwire.extended import ExtendedSession
from orionbelt.pgwire.scram import SCRAM_SHA_256, ScramError, ScramServerExchange

logger = logging.getLogger(__name__)


# Type alias for the query handler. The server passes the literal SQL
# string from the Query frame and the ``database`` parameter captured
# from the client's StartupMessage. Handlers return raw bytes — a
# sequence of one or more protocol frames terminated by the per-statement
# reply (CommandComplete or ErrorResponse). The caller appends
# ReadyForQuery. Steps 3+ extend this signature (parameters, portals).
QueryHandler = Callable[..., Awaitable[bytes]]


async def _hello_world_handler(
    sql: str,
    database: str,
    *,
    result_formats: tuple[int, ...] = (),
) -> bytes:
    """Fallback responder used when no router is wired in.

    Recognises ``SELECT 1`` so connectivity probes still work in
    tests/dev when the server has no SessionManager attached. Every
    other query gets a clear ErrorResponse pointing at the missing
    handler so misuse is loud rather than silent.
    """

    del database, result_formats  # unused — see SemanticRouter for the real path
    normalised = sql.strip().rstrip(";").strip().lower()
    if normalised == "select 1":
        return (
            protocol.build_row_description([("?column?", protocol.OID_INT4)])
            + protocol.build_data_row(["1"])
            + protocol.build_command_complete("SELECT 1")
        )
    return protocol.build_error_response(
        severity="ERROR",
        code="0A000",  # feature_not_supported
        message=(
            "pgwire server has no query handler attached. "
            "Construct a SemanticRouter (orionbelt.pgwire.router) and "
            "pass it as query_handler= when starting the server."
        ),
    )


class PgWireServer:
    """Owns the asyncio server object and the connection-cap semaphore.

    Use ``start()`` to bind the socket and ``stop()`` to drain. The
    server is reusable across tests — each ``start()`` returns the
    actual bound port, which is useful for ``port=0`` ephemeral binds.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        auth_mode: str = "trust",
        max_connections: int = 64,
        query_timeout_seconds: float = 60.0,
        auth_timeout_seconds: float = 10.0,
        query_handler: QueryHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_mode = auth_mode
        self.query_timeout = query_timeout_seconds
        self.auth_timeout = auth_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_connections)
        self._handler: QueryHandler = query_handler or _hello_world_handler
        self._server: asyncio.base_events.Server | None = None

    @property
    def bound_port(self) -> int:
        """Return the port the server actually bound to (post-start)."""

        if self._server is None:
            raise RuntimeError("PgWireServer not started")
        sockets = list(self._server.sockets or ())
        if not sockets:
            raise RuntimeError("PgWireServer has no bound sockets")
        return int(sockets[0].getsockname()[1])

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle_connection, host=self.host, port=self.port
        )
        bound = self.bound_port
        logger.info("pgwire listening on %s:%d (auth=%s)", self.host, bound, self.auth_mode)
        return bound

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("PgWireServer not started")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    # Maximum time we'll wait for the OS / client to drain a write
    # before declaring the connection dead. A client that stopped
    # reading (Tableau JDBC crashes / aborts mid-response) pins the
    # event loop indefinitely without this — and the process becomes
    # unresponsive to Ctrl+C until the socket OS-timeouts.
    _WRITE_DRAIN_TIMEOUT_SECONDS: float = 10.0

    async def _drain(self, writer: asyncio.StreamWriter) -> None:
        """``writer.drain()`` with a hard timeout so a dead client
        can't hang the server."""

        try:
            await asyncio.wait_for(
                writer.drain(),
                timeout=self._WRITE_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise ConnectionError("pgwire write drain timed out") from exc

    async def _send_fatal(self, writer: asyncio.StreamWriter, *, code: str, message: str) -> None:
        """Write a FATAL ErrorResponse and flush it."""
        writer.write(protocol.build_error_response(severity="FATAL", code=code, message=message))
        await self._drain(writer)

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> protocol.StartupMessage | None:
        """Read the StartupMessage (handling SSLRequest) and authenticate.

        Returns the StartupMessage on success, or ``None`` when auth was
        rejected (a FATAL ErrorResponse has already been written). Run under a
        wait_for deadline by the caller so a stalled client cannot pin a slot.
        """
        startup = await protocol.read_startup_message(reader.readexactly)
        if startup is None:
            # SSLRequest — reject (no in-process TLS), then read the real one.
            writer.write(b"N")
            await self._drain(writer)
            startup = await protocol.read_startup_message(reader.readexactly)
            if startup is None:
                raise protocol.ProtocolError("Repeated SSLRequest on same socket")

        if not await self._authenticate(reader, writer, startup):
            return None
        return startup

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        startup: protocol.StartupMessage,
    ) -> bool:
        """Run the auth handshake for the connection.

        Returns True when the connection is authenticated (and AuthenticationOk
        has NOT yet been written — the caller emits it). Returns False after
        writing a FATAL ErrorResponse on rejection.
        """
        if not auth_required():
            # trust mode — validate (always ok) for symmetry, no exchange.
            return authenticate(startup=startup, password=None).ok

        mechanism = select_mechanism(self.auth_mode)
        if mechanism == MECH_CLEARTEXT:
            return await self._run_cleartext(reader, writer, startup)
        return await self._run_scram(reader, writer)

    async def _run_cleartext(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        startup: protocol.StartupMessage,
    ) -> bool:
        writer.write(protocol.build_authentication_cleartext_password())
        await self._drain(writer)
        tag, body = await protocol.read_message(
            reader.readexactly, max_length=protocol.MAX_AUTH_FRAME_SIZE
        )
        if tag != b"p":
            await self._send_fatal(writer, code="08P01", message="expected PasswordMessage")
            return False
        password = protocol.parse_password_message(body)
        result = authenticate(startup=startup, password=password)
        if not result.ok:
            await self._send_fatal(
                writer, code="28P01", message=result.error_message or "authentication failed"
            )
            return False
        return True

    async def _run_scram(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Drive the server side of a SCRAM-SHA-256 SASL exchange."""
        exchange = ScramServerExchange(scram_candidate_keys())

        # 1. Advertise the mechanism.
        writer.write(protocol.build_authentication_sasl([SCRAM_SHA_256]))
        await self._drain(writer)

        # 2. SASLInitialResponse: mechanism selection + client-first-message.
        tag, body = await protocol.read_message(
            reader.readexactly, max_length=protocol.MAX_AUTH_FRAME_SIZE
        )
        if tag != b"p":
            await self._send_fatal(writer, code="08P01", message="expected SASLInitialResponse")
            return False
        mechanism, client_first = protocol.parse_sasl_initial_response(body)
        if mechanism != SCRAM_SHA_256:
            await self._send_fatal(
                writer, code="28P01", message=f"unsupported SASL mechanism: {mechanism}"
            )
            return False
        try:
            server_first = exchange.handle_client_first(client_first)
        except ScramError:
            await self._send_fatal(writer, code="08P01", message="malformed SCRAM client-first")
            return False
        writer.write(protocol.build_authentication_sasl_continue(server_first.encode("utf-8")))
        await self._drain(writer)

        # 3. SASLResponse: client-final-message with the proof.
        tag, body = await protocol.read_message(
            reader.readexactly, max_length=protocol.MAX_AUTH_FRAME_SIZE
        )
        if tag != b"p":
            await self._send_fatal(writer, code="08P01", message="expected SASLResponse")
            return False
        client_final = protocol.parse_sasl_response(body)
        try:
            server_final = exchange.handle_client_final(client_final)
        except ScramError:
            await self._send_fatal(writer, code="28P01", message="SCRAM authentication failed")
            return False

        # 4. SASLFinal with the server signature; AuthenticationOk follows.
        writer.write(protocol.build_authentication_sasl_final(server_final.encode("utf-8")))
        await self._drain(writer)
        return True

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        if self._semaphore.locked():
            logger.warning("pgwire rejecting connection from %s — at capacity", peer)
            await self._safe_send_error(
                writer,
                code="53300",  # too_many_connections
                message="pgwire connection limit reached",
            )
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return
        await self._semaphore.acquire()
        try:
            await self._drive_session(reader, writer, peer)
        except protocol.ProtocolError as exc:
            logger.info("pgwire protocol error from %s: %s", peer, exc)
            await self._safe_send_error(writer, code="08P01", message=str(exc))
        except (asyncio.IncompleteReadError, ConnectionError):
            logger.debug("pgwire connection from %s dropped", peer)
        except Exception:
            logger.exception("pgwire unhandled error from %s", peer)
        finally:
            self._semaphore.release()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _drive_session(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> None:
        # Bound the entire pre-auth handshake (startup read + credential
        # exchange) with a hard deadline. An unauthenticated client that
        # opens a socket and stalls would otherwise hold a connection slot
        # forever; with PGWIRE_MAX_CONNECTIONS such clients could exhaust
        # all slots and lock out legitimate users.
        try:
            startup = await asyncio.wait_for(
                self._handshake(reader, writer),
                timeout=self.auth_timeout,
            )
        except TimeoutError:
            logger.info("pgwire auth handshake timed out for %s", peer)
            await self._safe_send_error(
                writer,
                code="08006",  # connection_failure
                message="authentication handshake timed out",
            )
            return
        if startup is None:
            # SSLRequest loop exhausted or auth rejected — error already sent.
            return

        # Handshake complete: AuthOk + a small set of ParameterStatus
        # frames + BackendKeyData + ReadyForQuery.
        writer.write(protocol.build_authentication_ok())
        for name, value in _startup_parameters().items():
            writer.write(protocol.build_parameter_status(name, value))
        writer.write(
            protocol.build_backend_key_data(pid=_fake_backend_pid(), secret=secrets.randbits(31))
        )
        writer.write(protocol.build_ready_for_query())
        await self._drain(writer)

        logger.info(
            "pgwire session opened peer=%s user=%s database=%s",
            peer,
            startup.user,
            startup.database,
        )

        # Per-connection extended-query state.  Each Bind eagerly runs
        # the query through ``self._handler``; the cached reply replays
        # for the matching Describe + Execute pair.
        extended = ExtendedSession(handler=self._handler, database=startup.database)
        skip_until_sync = False

        while True:
            tag, body = await protocol.read_message(reader.readexactly)
            if tag == b"X":  # Terminate
                return
            if tag == b"Q":
                # A Simple Query implicitly closes any pending extended
                # transaction; reset the skip-until-Sync flag so errors
                # don't leak between modes.
                skip_until_sync = False
                query = protocol.parse_query(body)
                try:
                    reply = await asyncio.wait_for(
                        self._handler(query.sql, startup.database),
                        timeout=self.query_timeout,
                    )
                    writer.write(reply)
                except TimeoutError:
                    writer.write(
                        protocol.build_error_response(
                            severity="ERROR",
                            code="57014",  # query_canceled
                            message=(f"pgwire query exceeded timeout of {self.query_timeout:g}s"),
                        )
                    )
                writer.write(protocol.build_ready_for_query())
                await self._drain(writer)
                continue

            # ---- Extended query protocol --------------------------------
            if tag == b"S":  # Sync — emit ReadyForQuery, clear skip flag
                skip_until_sync = False
                writer.write(protocol.build_ready_for_query())
                await self._drain(writer)
                continue

            if skip_until_sync:
                # PostgreSQL behaviour: after an error the server drops
                # extended-query messages until the client catches up
                # with Sync.  Flush is allowed but no-ops.
                continue

            if tag == b"H":  # Flush — no-op; we already drain after each reply
                await self._drain(writer)
                continue

            try:
                reply_bytes = await self._dispatch_extended(tag, body, extended)
            except protocol.ProtocolError as exc:
                reply_bytes = protocol.build_error_response(
                    severity="ERROR",
                    code="08P01",  # protocol_violation
                    message=str(exc),
                )

            writer.write(reply_bytes)
            await self._drain(writer)

            # Any ErrorResponse in extended-query mode triggers the
            # skip-until-Sync state described above.
            if _contains_error_response(reply_bytes):
                skip_until_sync = True

    async def _dispatch_extended(
        self,
        tag: bytes,
        body: bytes,
        extended: ExtendedSession,
    ) -> bytes:
        if tag == b"P":
            return await asyncio.wait_for(
                extended.parse(protocol.parse_parse(body)),
                timeout=self.query_timeout,
            )
        if tag == b"B":
            return await asyncio.wait_for(
                extended.bind(protocol.parse_bind(body)),
                timeout=self.query_timeout,
            )
        if tag == b"D":
            return extended.describe(protocol.parse_describe(body))
        if tag == b"E":
            return extended.execute(protocol.parse_execute(body))
        if tag == b"C":
            return extended.close(protocol.parse_close(body))
        return protocol.build_error_response(
            severity="ERROR",
            code="0A000",
            message=f"pgwire frame tag {tag!r} is not implemented",
        )

    async def _safe_send_error(
        self,
        writer: asyncio.StreamWriter,
        *,
        code: str,
        message: str,
    ) -> None:
        try:
            writer.write(
                protocol.build_error_response(severity="FATAL", code=code, message=message)
            )
            await self._drain(writer)
        except Exception:
            pass


def _contains_error_response(reply: bytes) -> bool:
    """Return ``True`` when any frame in ``reply`` is an ErrorResponse (``E``).

    Extended-query mode requires the server to enter skip-until-Sync
    after any error. A bare ``startswith(b"E")`` check is not enough —
    ``Describe('S')`` returns ``ParameterDescription`` (``t``) followed
    by ``ErrorResponse`` (``E``) when the prepared statement is
    invalid, and other compound replies follow the same shape. Walk
    the frame boundaries by length prefix so the ``E`` is found
    wherever it lands.

    Postgres frame layout: 1-byte tag + 4-byte big-endian length (the
    length field includes the four length bytes themselves).
    """

    offset = 0
    end = len(reply)
    while offset + 5 <= end:
        if reply[offset : offset + 1] == b"E":
            return True
        (length,) = struct.unpack("!I", reply[offset + 1 : offset + 5])
        if length < 4:
            # Malformed / truncated frame — stop scanning rather than
            # spin forever; the caller will see whatever frames we did
            # parse.
            return False
        offset += 1 + length
    return False


def _startup_parameters() -> dict[str, str]:
    """The ParameterStatus payload reported to clients after AuthOk.

    These mirror real Postgres values enough for BI tool driver
    handshakes. Step 3's catalog emulation expands the set.

    ``search_path`` is included because pgjdbc 42.x reads it from the
    cached startup parameters via
    ``serverParameters.get("search_path").toString()``. Real Postgres
    always emits a ``ParameterStatus`` for ``search_path`` after
    AuthOk; without one the lookup returns ``null`` and the
    ``.toString()`` call NPEs with
    "Cannot invoke java.lang.CharSequence.toString() because
    <parameter1> is null" — the DBeaver-on-every-activity NPE the
    user kept hitting after the v2.5.0 layout flip. The value uses
    the same ``"$user"`` macro real Postgres ships so clients don't
    treat it as a literal schema name.
    """

    return {
        "server_version": "15.0 (orionbelt-pgwire 0.1)",
        "server_encoding": "UTF8",
        "client_encoding": "UTF8",
        "DateStyle": "ISO, MDY",
        "TimeZone": "UTC",
        "integer_datetimes": "on",
        "standard_conforming_strings": "on",
        "application_name": "",
        "search_path": '"$user", public',
        "is_superuser": "off",
        "session_authorization": "obsl",
        "IntervalStyle": "postgres",
    }


_BACKEND_PID_BASE = 10_000


def _fake_backend_pid() -> int:
    """Stable-ish PID-shaped int for BackendKeyData.

    Real Postgres uses an OS PID. We don't need cancel support yet, so
    a 31-bit random value is fine — the cancel key is paired with this
    PID in CancelRequest, which Step 1 doesn't honour.
    """

    return _BACKEND_PID_BASE + secrets.randbelow(50_000)
