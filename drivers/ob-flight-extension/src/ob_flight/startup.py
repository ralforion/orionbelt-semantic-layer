"""Flight server lifecycle management — daemon thread startup/shutdown."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("ob_flight.startup")

_server: Any = None
_thread: threading.Thread | None = None


def start_flight_background(
    *,
    session_manager: Any = None,
    port: int | None = None,
    auth_handler: Any = None,
    default_dialect: str | None = None,
) -> threading.Thread:
    """Launch the Flight SQL server in a daemon thread.

    Parameters
    ----------
    session_manager : SessionManager
        The shared SessionManager from the FastAPI lifespan.
    port : int, optional
        gRPC port (default: FLIGHT_PORT env var or 8815).
    auth_handler : ServerAuthHandler, optional
        Auth handler (default: created from FLIGHT_AUTH_MODE env var).
    default_dialect : str, optional
        DB dialect (default: DB_VENDOR env var or "duckdb").
    """
    global _server, _thread

    from ob_flight.server import OBFlightServer

    if auth_handler is None:
        from ob_flight.auth import create_auth_handler

        auth_handler = create_auth_handler()

    if port is None:
        port = int(os.getenv("FLIGHT_PORT", "8815"))

    if default_dialect is None:
        default_dialect = os.getenv("DB_VENDOR", "duckdb")
    location = f"grpc://0.0.0.0:{port}"

    _server = OBFlightServer(
        location,
        auth_handler=auth_handler,
        session_manager=session_manager,
        default_dialect=default_dialect,
    )

    _thread = threading.Thread(
        target=_server.serve,
        name="ob-flight-server",
        daemon=True,
    )
    _thread.start()
    logger.info("Flight SQL server started on port %d (dialect=%s)", port, default_dialect)
    return _thread


def stop_flight_server() -> None:
    """Shutdown the Flight server, with a timeout to avoid blocking forever.

    pyarrow's FlightServerBase.shutdown() blocks until all in-flight requests
    finish.  DBeaver (and other JDBC clients) keep idle connections open, so
    shutdown() can hang indefinitely.  We call it from a helper thread and
    join with a short timeout so the main process can exit cleanly.
    """
    global _server, _thread
    if _server is not None:
        server = _server
        # Call shutdown() from a helper thread — it blocks and we don't want
        # to stall the FastAPI lifespan finalizer.
        shutdown_thread = threading.Thread(
            target=_shutdown_safely, args=(server,), daemon=True
        )
        shutdown_thread.start()
        shutdown_thread.join(timeout=3)
        # Wait for the serve() thread to finish too
        if _thread is not None and _thread.is_alive():
            _thread.join(timeout=2)
        _server = None
        _thread = None
        logger.info("Flight SQL server stopped")


def _shutdown_safely(server: Any) -> None:
    """Call server.shutdown() in a thread-safe way."""
    try:
        server.shutdown()
    except Exception:
        pass
