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
from collections.abc import Awaitable, Callable

from orionbelt.pgwire import protocol
from orionbelt.pgwire.auth import authenticate
from orionbelt.pgwire.extended import ExtendedSession

logger = logging.getLogger(__name__)


# Type alias for the query handler. The server passes the literal SQL
# string from the Query frame and the ``database`` parameter captured
# from the client's StartupMessage. Handlers return raw bytes — a
# sequence of one or more protocol frames terminated by the per-statement
# reply (CommandComplete or ErrorResponse). The caller appends
# ReadyForQuery. Steps 3+ extend this signature (parameters, portals).
QueryHandler = Callable[[str, str], Awaitable[bytes]]


async def _hello_world_handler(sql: str, database: str) -> bytes:
    """Fallback responder used when no router is wired in.

    Recognises ``SELECT 1`` so connectivity probes still work in
    tests/dev when the server has no SessionManager attached. Every
    other query gets a clear ErrorResponse pointing at the missing
    handler so misuse is loud rather than silent.
    """

    del database  # unused — see SemanticRouter for the real path
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
        query_handler: QueryHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_mode = auth_mode
        self.query_timeout = query_timeout_seconds
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
        startup = await protocol.read_startup_message(reader.readexactly)
        if startup is None:
            # SSLRequest — reject (Step 1 has no TLS in-process), then
            # read the real StartupMessage on the same socket.
            writer.write(b"N")
            await writer.drain()
            startup = await protocol.read_startup_message(reader.readexactly)
            if startup is None:
                raise protocol.ProtocolError("Repeated SSLRequest on same socket")

        auth = authenticate(auth_mode=self.auth_mode, startup=startup)
        if not auth.ok:
            writer.write(
                protocol.build_error_response(
                    severity="FATAL",
                    code="28P01",  # invalid_password
                    message=auth.error_message or "authentication failed",
                )
            )
            await writer.drain()
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
        await writer.drain()

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
                await writer.drain()
                continue

            # ---- Extended query protocol --------------------------------
            if tag == b"S":  # Sync — emit ReadyForQuery, clear skip flag
                skip_until_sync = False
                writer.write(protocol.build_ready_for_query())
                await writer.drain()
                continue

            if skip_until_sync:
                # PostgreSQL behaviour: after an error the server drops
                # extended-query messages until the client catches up
                # with Sync.  Flush is allowed but no-ops.
                continue

            if tag == b"H":  # Flush — no-op; we already drain after each reply
                await writer.drain()
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
            await writer.drain()

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
            return extended.parse(protocol.parse_parse(body))
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
            await writer.drain()
        except Exception:
            pass


def _contains_error_response(reply: bytes) -> bool:
    """Quick scan: does this reply contain an ErrorResponse (``E``) frame?

    Extended-query mode requires the server to enter skip-until-Sync
    after any error; the router emits errors as a single ``E`` frame so
    a one-byte check is enough.
    """

    return reply.startswith(b"E")


def _startup_parameters() -> dict[str, str]:
    """The ParameterStatus payload reported to clients after AuthOk.

    These mirror real Postgres values enough for BI tool driver
    handshakes. Step 3's catalog emulation expands the set.
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
    }


_BACKEND_PID_BASE = 10_000


def _fake_backend_pid() -> int:
    """Stable-ish PID-shaped int for BackendKeyData.

    Real Postgres uses an OS PID. We don't need cancel support yet, so
    a 31-bit random value is fine — the cancel key is paired with this
    PID in CancelRequest, which Step 1 doesn't honour.
    """

    return _BACKEND_PID_BASE + secrets.randbelow(50_000)
