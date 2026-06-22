"""AppRuntime ownership (Phase 4).

``create_app`` consolidates per-app runtime state into a single
:class:`AppRuntime` attached to ``app.state.runtime``. These tests confirm
each app instance owns its own runtime object carrying its own config —
the foundation for embedding/instantiating apps without sharing a tangle
of module globals.
"""

from __future__ import annotations

from fastapi import FastAPI

from orionbelt.api.app import create_app
from orionbelt.api.deps import AppRuntime, reset_session_manager
from orionbelt.settings import Settings


def _app(db_vendor: str) -> FastAPI:
    settings = Settings(
        db_vendor=db_vendor,
        # No network surfaces — we only test runtime ownership, and two apps
        # would otherwise collide on the Flight / pgwire ports.
        flight_enabled=False,
        pgwire_enabled=False,
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
    )
    return create_app(settings=settings)


async def test_app_owns_its_runtime() -> None:
    app = _app("postgres")
    try:
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            assert isinstance(runtime, AppRuntime)
            assert runtime.db_vendor == "postgres"
            assert runtime.session_manager is not None
    finally:
        reset_session_manager()


async def test_two_apps_have_distinct_runtimes() -> None:
    app1 = _app("postgres")
    app2 = _app("mysql")
    try:
        async with (
            app1.router.lifespan_context(app1),
            app2.router.lifespan_context(app2),
        ):
            r1 = app1.state.runtime
            r2 = app2.state.runtime
            # Each app captured its own runtime object and config.
            assert r1 is not r2
            assert r1.db_vendor == "postgres"
            assert r2.db_vendor == "mysql"
    finally:
        reset_session_manager()
