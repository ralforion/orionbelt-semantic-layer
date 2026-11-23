"""The Flight auth handler is chosen from Settings and reported accurately.

The handler is constructed from Settings in the lifespan and always passed
explicitly, so the "WITHOUT authentication" warning and the actual handler
can never disagree. The removed FLIGHT_AUTH_MODE / FLIGHT_API_TOKEN settings
refuse to start rather than being ignored.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("ob_flight", reason="Flight extension required")

import ob_flight.auth as ob_auth  # noqa: E402
import ob_flight.startup as ob_startup  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.auth import AuthConfigError  # noqa: E402
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


async def test_no_auth_uses_noop_and_warns(monkeypatch, caplog) -> None:
    settings = Settings(flight_enabled=True, pgwire_enabled=False, api_server_port=0)
    caplog.set_level(logging.WARNING, logger="orionbelt.api")
    captured = await _run_lifespan_capture(monkeypatch, settings)

    assert isinstance(captured["auth_handler"], ob_auth.NoopAuthHandler)
    assert any("WITHOUT authentication" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    ("removed", "named"),
    [
        ({"auth_enabled": True}, "AUTH_ENABLED"),
        ({"flight_auth_mode": "token", "flight_api_token": "some-token"}, "FLIGHT_AUTH_MODE"),
        ({"flight_api_token": "some-token"}, "FLIGHT_API_TOKEN"),
    ],
)
def test_a_removed_setting_that_would_turn_auth_on_refuses_to_start(
    removed: dict, named: str
) -> None:
    # Ignoring it would start the server without the auth it asked for.
    with pytest.raises(AuthConfigError, match=named):
        create_app(settings=Settings(flight_enabled=True, **removed))


def test_flight_auth_mode_none_is_harmless() -> None:
    create_app(settings=Settings(flight_enabled=True, flight_auth_mode="none"))


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

    # The handler is only half of it. Header auth - what every current client
    # speaks - lives in middleware, and the two must be installed together:
    # alone, the handler refuses every header-authenticated call and the
    # middleware refuses the legacy handshake. Asserting only the handler is
    # what let that ship.
    middleware = captured["auth_middleware"]
    assert ob_auth.AUTH_MIDDLEWARE_KEY in middleware
    assert isinstance(middleware[ob_auth.AUTH_MIDDLEWARE_KEY], ob_auth.AuthMiddlewareFactory)


async def test_other_modes_install_no_auth_middleware(monkeypatch) -> None:
    """The middleware exists for api_key mode only, so an unauthenticated
    surface pays nothing per call."""
    settings = Settings(
        flight_enabled=True,
        pgwire_enabled=False,
        api_server_port=0,
    )
    captured = await _run_lifespan_capture(monkeypatch, settings)

    assert not captured["auth_middleware"]


async def test_oidc_mode_installs_shared_auth_without_api_keys(monkeypatch, tmp_path) -> None:
    """Under oidc, Flight takes the IdP's tokens, so it needs no API_KEYS."""
    import json

    from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(FakeIssuer().jwks()))
    settings = Settings(
        flight_enabled=True,
        auth_mode="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_file=str(jwks),
        pgwire_enabled=False,
        api_server_port=0,
    )
    captured = await _run_lifespan_capture(monkeypatch, settings)

    assert isinstance(captured["auth_handler"], ob_auth.SharedKeyAuthHandler)
    assert ob_auth.AUTH_MIDDLEWARE_KEY in captured["auth_middleware"]
