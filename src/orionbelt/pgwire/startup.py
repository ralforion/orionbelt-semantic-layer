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

from orionbelt.pgwire.server import PgWireServer, QueryHandler
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
    query_handler: QueryHandler | None = None,
) -> PgWireRuntime | None:
    """Bind the pgwire TCP socket and spawn the ``serve_forever`` task.

    Returns ``None`` if ``PGWIRE_ENABLED`` is false. The Step 1 default
    ``query_handler`` answers only ``SELECT 1`` — callers in Step 2+
    pass a router bound to the active ``SessionManager``.
    """

    if not settings.pgwire_enabled:
        return None

    server = PgWireServer(
        host=settings.pgwire_host,
        port=settings.pgwire_port,
        auth_mode=settings.pgwire_auth_mode,
        max_connections=settings.pgwire_max_connections,
        query_timeout_seconds=float(settings.pgwire_query_timeout_seconds),
        query_handler=query_handler,
    )
    bound = await server.start()
    task = asyncio.create_task(server.serve_forever(), name="pgwire-server")
    logger.info(
        "pgwire surface enabled on %s:%d (auth=%s)",
        settings.pgwire_host,
        bound,
        settings.pgwire_auth_mode,
    )
    return PgWireRuntime(server=server, task=task)
