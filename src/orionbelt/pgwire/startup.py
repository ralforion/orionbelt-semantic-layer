"""FastAPI lifespan integration for the pgwire surface.

Unlike the Flight server (which runs in a background thread because
``pyarrow.flight`` is blocking), pgwire is asyncio-native so it shares
the FastAPI event loop directly. Step 1 wires only the start/stop seam;
Steps 2+ pass a real router as the ``query_handler``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer, QueryHandler
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

logger = logging.getLogger(__name__)


class PgWireRuntime:
    """Holds the server + the asyncio task running ``serve_forever``."""

    def __init__(self, server: PgWireServer, task: asyncio.Task[None]) -> None:
        self.server = server
        self.task = task

    async def shutdown(self) -> None:
        await self.server.stop()
        if not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task


async def start_pgwire(
    settings: Settings,
    *,
    session_manager: SessionManager | None = None,
    query_handler: QueryHandler | None = None,
    cache: object | None = None,
    cache_config: object | None = None,
) -> PgWireRuntime | None:
    """Bind the pgwire TCP socket and spawn the ``serve_forever`` task.

    Returns ``None`` if ``PGWIRE_ENABLED`` is false. When
    ``query_handler`` is omitted and a ``session_manager`` is supplied
    we build a :class:`SemanticRouter` bound to the manager — that is
    the production path used by the FastAPI lifespan. Tests that don't
    need semantic routing can pass a stub handler directly.
    """

    if not settings.pgwire_enabled:
        return None

    handler: QueryHandler | None = query_handler
    if handler is None and session_manager is not None:
        router = SemanticRouter(
            session_manager=session_manager,
            default_dialect=settings.db_vendor,
            cache=cache,
            cache_config=cache_config,
        )
        handler = router.handle

    server = PgWireServer(
        host=settings.pgwire_host,
        port=settings.pgwire_port,
        auth_mode=settings.pgwire_auth_mode,
        max_connections=settings.pgwire_max_connections,
        query_timeout_seconds=float(settings.pgwire_query_timeout_seconds),
        auth_timeout_seconds=float(settings.pgwire_auth_timeout_seconds),
        query_handler=handler,
    )
    bound = await server.start()
    task = asyncio.create_task(server.serve_forever(), name="pgwire-server")
    logger.info(
        "pgwire surface enabled on %s:%d (auth=%s, semantic=%s)",
        settings.pgwire_host,
        bound,
        settings.pgwire_auth_mode,
        handler is not None,
    )
    return PgWireRuntime(server=server, task=task)
