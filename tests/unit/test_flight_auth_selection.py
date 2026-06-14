"""The Flight auth handler is chosen from Settings and reported accurately.

Regression guard for the review finding: FLIGHT_API_TOKEN set WITHOUT
FLIGHT_AUTH_MODE=token used to suppress the "WITHOUT authentication" warning
while the handler was still NoopAuthHandler. The handler is now constructed
from Settings in the lifespan and always passed explicitly, so the warning and
the actual handler can never disagree.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("ob_flight", reason="Flight extension required")

import ob_flight.auth as ob_auth  # noqa: E402
import ob_flight.startup as ob_startup  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402

STRONG_KEY = "obsl_pat_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


@pytest.fixture(autouse=True)
def _cleanup():
    """Reset global session-manager / auth state after each lifespan run.

    The fail-closed test raises before the lifespan reaches its own cleanup, so
    reset here to avoid leaking state into other tests.
    """
    yield
    from orionbelt.api.deps import reset_session_manager
    from orionbelt.auth import reset_auth

    reset_session_manager()
    reset_auth()


async def _run_lifespan_capture(monkeypatch, settings: Settings) -> dict:
    """Drive the lifespan with a stubbed start_flight_background; return kwargs."""
    captured: dict = {}

    def _recorder(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(ob_startup, "start_flight_background", _recorder)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        pass
    return captured


async def test_token_set_without_token_mode_uses_noop_and_warns(monkeypatch, caplog) -> None:
    # FLIGHT_API_TOKEN present but FLIGHT_AUTH_MODE != token -> the token is
    # ignored, so the surface is unauthenticated and MUST warn.
    settings = Settings(
        flight_enabled=True,
        flight_api_token="some-token-value",
        flight_auth_mode="none",
        pgwire_enabled=False,
        api_server_port=0,
    )
    caplog.set_level(logging.WARNING, logger="orionbelt.api")
    captured = await _run_lifespan_capture(monkeypatch, settings)

    assert isinstance(captured["auth_handler"], ob_auth.NoopAuthHandler)
    assert any("WITHOUT authentication" in r.message for r in caplog.records)


async def test_token_mode_uses_token_handler(monkeypatch, caplog) -> None:
    settings = Settings(
        flight_enabled=True,
        flight_api_token="some-token-value",
        flight_auth_mode="token",
        pgwire_enabled=False,
        api_server_port=0,
    )
    caplog.set_level(logging.WARNING, logger="orionbelt.api")
    captured = await _run_lifespan_capture(monkeypatch, settings)

    assert isinstance(captured["auth_handler"], ob_auth.TokenAuthHandler)
    # No "unauthenticated" warning when a real handler is configured.
    assert not any("WITHOUT authentication" in r.message for r in caplog.records)


async def test_token_mode_without_token_fails_closed(monkeypatch) -> None:
    # FLIGHT_AUTH_MODE=token but no token -> must fail closed at startup, never
    # silently downgrade to NoopAuthHandler.
    captured: dict = {}

    def _recorder(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(ob_startup, "start_flight_background", _recorder)
    settings = Settings(
        flight_enabled=True,
        flight_auth_mode="token",
        flight_api_token=None,
        pgwire_enabled=False,
        api_server_port=0,
    )
    app = create_app(settings=settings)
    with pytest.raises(ValueError, match="FLIGHT_API_TOKEN"):
        async with app.router.lifespan_context(app):
            pass
    assert "auth_handler" not in captured  # Flight must not have been started


async def test_api_key_mode_uses_shared_handler(monkeypatch, caplog) -> None:
    settings = Settings(
        flight_enabled=True,
        auth_mode="api_key",
        api_keys=STRONG_KEY,
        pgwire_enabled=False,
        api_server_port=0,
    )
    caplog.set_level(logging.WARNING, logger="orionbelt.api")
    captured = await _run_lifespan_capture(monkeypatch, settings)

    assert isinstance(captured["auth_handler"], ob_auth.SharedKeyAuthHandler)
    assert not any("WITHOUT authentication" in r.message for r in caplog.records)
