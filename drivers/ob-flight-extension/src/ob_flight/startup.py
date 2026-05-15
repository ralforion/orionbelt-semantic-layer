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
    cache: Any = None,
    cache_config: Any = None,
) -> threading.Thread:
    """Launch the Flight SQL server in a daemon thread.

    Governance is hard-coded in v2.4.0+: OBSL is a semantic layer, not a
    JDBC proxy. Raw SQL pass-through is **not configurable** — it always
    rejects with ``RAW_SQL_REJECTED``. DDL/DML always reject with
    ``WRITE_OPERATION_REJECTED``. Catalog discovery (SHOW / DESCRIBE /
    information_schema / pg_catalog) is answered from the model in-process
    and never touches the warehouse. See ``design/PLAN_flight_natural_sql.md``
    §3.2 for the full mode table.
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
        cache=cache,
        cache_config=cache_config,
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
    """Shutdown the Flight server and release the bound port.

    pyarrow's ``FlightServerBase.shutdown()`` blocks until all in-flight
    requests finish. DBeaver / other JDBC clients keep idle gRPC
    connections open, so the call can hang indefinitely. Strategy:

    1. Call ``shutdown()`` from a helper thread with a short join timeout
       so the FastAPI lifespan finalizer doesn't stall.
    2. From the same helper, call ``wait()`` — that's what actually
       drains the gRPC C++ thread and unbinds port 8815. Without it,
       a quick API restart hits ``Address already in use``.
    3. If serve() still hasn't returned after both timeouts, log a
       warning. The daemon Python thread dies with the process; the
       kernel reclaims the socket on actual process exit.
    """
    global _server, _thread
    if _server is None:
        return

    server = _server
    shutdown_thread = threading.Thread(target=_shutdown_safely, args=(server,), daemon=True)
    shutdown_thread.start()
    shutdown_thread.join(timeout=5)

    # Wait for serve() to return — that's what releases the bound port.
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=3)

    if _thread is not None and _thread.is_alive():
        logger.warning(
            "Flight server did not stop within 8s. The port may stay bound "
            "until process exit. Tip: `lsof -ti :8815 | xargs kill -9` "
            "if a stale listener blocks the next startup."
        )

    _server = None
    _thread = None
    logger.info("Flight SQL server stopped")


def _shutdown_safely(server: Any) -> None:
    """Call shutdown() then wait() in a thread-safe way.

    ``wait()`` is the call that actually releases the bound port — without
    it, ``shutdown()`` returns but the gRPC C++ thread can keep the socket.
    """
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.wait()
    except Exception:
        pass
