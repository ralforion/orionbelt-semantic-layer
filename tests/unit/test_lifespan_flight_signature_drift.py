"""Runtime regression guard for issue #96.

OBSL has called ``start_flight_background(cache=cache,
cache_config=cache_config)`` since v2.4.0, but the published PyPI
release of ob-flight-extension (2.1.0) lagged behind and has a
signature that does not accept those kwargs. Before the v2.7.8 fix,
this raised ``TypeError`` inside the FastAPI lifespan and crashed the
whole API startup, breaking the published Colab quickstart on every
fresh install. v2.7.8 catches ``TypeError`` in addition to
``ImportError`` so the API continues to serve REST / pgwire even when
Flight SQL cannot start.

This file directly exercises that fallback at runtime by monkeypatching
``ob_flight.startup.start_flight_background`` to a stub that mimics the
old PyPI signature (raises ``TypeError`` on ``cache=``), then drives
the FastAPI lifespan and asserts the API still comes up.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture
def _fake_ob_flight_with_old_signature(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a fake ``ob_flight.startup`` module whose
    ``start_flight_background`` refuses ``cache`` / ``cache_config``
    kwargs - the same TypeError the published PyPI 2.1.0 raises.

    Also forces ``importlib.util.find_spec("ob_flight")`` to truthy so
    the lifespan's flight-startup branch runs (otherwise the branch is
    skipped and we'd never exercise the fix).
    """
    import types

    fake_pkg = types.ModuleType("ob_flight")
    fake_pkg.__version__ = "2.1.0-fake"  # type: ignore[attr-defined]
    fake_startup = types.ModuleType("ob_flight.startup")

    def _old_signature(
        *,
        session_manager: Any = None,
        port: int | None = None,
        auth_handler: Any = None,
        default_dialect: str | None = None,
    ) -> object:
        # Mirror the real PyPI 2.1.0 signature - no cache / cache_config.
        raise AssertionError(
            "Stub should never actually be called - the kwarg-mismatch "
            "TypeError must fire before reaching the body."
        )

    fake_startup.start_flight_background = _old_signature  # type: ignore[attr-defined]
    fake_startup.stop_flight_server = lambda: None  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "ob_flight", fake_pkg)
    monkeypatch.setitem(sys.modules, "ob_flight.startup", fake_startup)

    # Force find_spec to see the fake package so the lifespan enters
    # the Flight branch even though no real ob_flight is installed.
    real_find_spec = importlib.util.find_spec

    def _find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "ob_flight":
            return importlib.machinery.ModuleSpec("ob_flight", loader=None)
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    yield


@pytest.mark.asyncio
async def test_lifespan_survives_flight_kwarg_mismatch(
    _fake_ob_flight_with_old_signature: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drive the FastAPI lifespan against a stale ob-flight-extension
    signature. Pre-v2.7.8 this raised ``TypeError`` and crashed startup;
    post-fix the lifespan must log a warning naming the kwarg mismatch
    and continue serving the API (issue #96).
    """
    # Flight now starts only when FLIGHT_ENABLED=true (no package-presence
    # auto-start), so enable it explicitly to exercise the startup branch.
    # Also disable pgwire so we don't try to start anything else.
    monkeypatch.setenv("FLIGHT_ENABLED", "true")
    monkeypatch.setenv("PGWIRE_ENABLED", "false")
    monkeypatch.setenv("API_SERVER_PORT", "0")
    monkeypatch.setenv("DISABLE_SESSION_LIST", "false")

    from orionbelt.api.app import create_app

    app = create_app()

    import logging

    caplog.set_level(logging.WARNING, logger="orionbelt.api.app")

    # Enter and exit the lifespan exactly as uvicorn would.
    async with app.router.lifespan_context(app):
        pass  # lifespan started successfully despite the kwarg mismatch

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Flight SQL startup skipped" in m for m in warnings), (
        "Expected the lifespan to warn about the ob-flight-extension "
        "kwarg mismatch instead of crashing. See #96. "
        f"Got warnings: {warnings}"
    )


@pytest.mark.asyncio
async def test_flight_does_not_autostart_without_flag(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flight must NOT start by package presence alone (requires FLIGHT_ENABLED)."""
    import types

    called = {"start": False}

    fake_pkg = types.ModuleType("ob_flight")
    fake_startup = types.ModuleType("ob_flight.startup")

    def _recorder(**_kwargs: Any) -> object:
        called["start"] = True
        return object()

    fake_startup.start_flight_background = _recorder  # type: ignore[attr-defined]
    fake_startup.stop_flight_server = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ob_flight", fake_pkg)
    monkeypatch.setitem(sys.modules, "ob_flight.startup", fake_startup)

    real_find_spec = importlib.util.find_spec

    def _find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "ob_flight":
            return importlib.machinery.ModuleSpec("ob_flight", loader=None)
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)

    import logging

    from orionbelt.api.app import create_app
    from orionbelt.settings import Settings

    # Explicit Settings so the result is hermetic regardless of any local .env
    # (constructor kwargs override env / .env). flight_enabled=False is the
    # behaviour under test: the package is "present" but the flag is off.
    settings = Settings(flight_enabled=False, pgwire_enabled=False, api_server_port=0)
    caplog.set_level(logging.INFO, logger="orionbelt.api")
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        pass

    assert called["start"] is False, "Flight must not auto-start without FLIGHT_ENABLED=true"
    assert any("FLIGHT_ENABLED is not set" in r.message for r in caplog.records)


def test_fix_predates_published_pypi_signature() -> None:
    """Sanity check: the OBSL call site really does pass kwargs that the
    PyPI release of ob-flight-extension (2.1.0) lacks. If this assertion
    ever fails because the PyPI release catches up, the TypeError-catch
    becomes belt-and-braces - keep it anyway as a forward-compat guard.
    """
    from pathlib import Path

    app_py = Path(__file__).resolve().parents[2] / "src" / "orionbelt" / "api" / "app.py"
    src = app_py.read_text(encoding="utf-8")
    assert "cache=cache" in src, "call site should pass cache=cache"
    assert "cache_config=cache_config" in src, "call site should pass cache_config=cache_config"


# Avoid colliding with the autoload fixture in conftest by being explicit.
os.environ.setdefault("ORIONBELT_TEST_NO_AUTO_API", "1")
