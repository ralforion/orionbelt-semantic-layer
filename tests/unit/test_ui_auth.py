"""UI auth-credential forwarding (design/PLAN_authentication.md Phase 3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

httpx = pytest.importorskip("httpx", reason="httpx required to drive the UI client")

from orionbelt.ui import app as ui_app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_headers():
    # Snapshot and restore the module-level header dict around each test.
    original = dict(ui_app._API_HEADERS)
    yield
    ui_app._API_HEADERS.clear()
    ui_app._API_HEADERS.update(original)


def test_set_api_credentials_adds_header() -> None:
    ui_app.set_api_credentials("obsl_pat_key_123456789012345678", "X-API-Key")
    assert ui_app._API_HEADERS["X-API-Key"] == "obsl_pat_key_123456789012345678"


def test_set_api_credentials_custom_header() -> None:
    ui_app.set_api_credentials("the-key-1234567890", "X-Custom-Auth")
    assert ui_app._API_HEADERS["X-Custom-Auth"] == "the-key-1234567890"


def test_set_api_credentials_none_clears() -> None:
    ui_app.set_api_credentials("the-key-1234567890", "X-API-Key")
    ui_app.set_api_credentials(None, "X-API-Key")
    assert "X-API-Key" not in ui_app._API_HEADERS


def test_set_api_credentials_is_idempotent() -> None:
    ui_app.set_api_credentials("first-key-1234567890", "X-API-Key")
    ui_app.set_api_credentials("second-key-123456789", "X-API-Key")
    # Only the latest key remains — no duplicate/stale header entries.
    keys = [k for k in ui_app._API_HEADERS if k.lower() == "x-api-key"]
    assert keys == ["X-API-Key"]
    assert ui_app._API_HEADERS["X-API-Key"] == "second-key-123456789"


def test_warn_when_api_requires_auth_and_no_key(capsys) -> None:
    fake = type("R", (), {"json": lambda self: {"auth_mode": "api_key"}})()
    with patch.object(ui_app.httpx, "get", return_value=fake):
        ui_app._warn_if_auth_required_without_key("http://api", None)
    out = capsys.readouterr().out
    assert "OBSL_API_KEY" in out and "api_key" in out


def test_no_warn_when_key_present(capsys) -> None:
    with patch.object(ui_app.httpx, "get") as mock_get:
        ui_app._warn_if_auth_required_without_key("http://api", "have-a-key")
    mock_get.assert_not_called()  # short-circuits before probing
    assert capsys.readouterr().out == ""


def test_no_warn_when_auth_disabled(capsys) -> None:
    fake = type("R", (), {"json": lambda self: {"auth_mode": "none"}})()
    with patch.object(ui_app.httpx, "get", return_value=fake):
        ui_app._warn_if_auth_required_without_key("http://api", None)
    assert capsys.readouterr().out == ""


def test_unreachable_api_does_not_warn(capsys) -> None:
    with patch.object(ui_app.httpx, "get", side_effect=httpx.ConnectError("down")):
        ui_app._warn_if_auth_required_without_key("http://api", None)
    assert capsys.readouterr().out == ""


# --- embedded UI must NOT auto-inject the server's API key (security) ---


def _create_app_with_auth(monkeypatch, obsl_api_key):
    """create_app in api_key mode with OBSL_API_KEY set or unset."""
    from orionbelt.api.app import create_app
    from orionbelt.settings import Settings

    if obsl_api_key is None:
        monkeypatch.delenv("OBSL_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OBSL_API_KEY", obsl_api_key)
    settings = Settings(
        auth_mode="api_key",
        api_keys="obsl_pat_server_key_0123456789abcdef",
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
    )
    create_app(settings=settings)


def test_embedded_ui_does_not_autoinject_server_key(monkeypatch) -> None:
    # With auth on and no OBSL_API_KEY, the embedded UI must NOT silently load
    # the server's API key (that would make /ui an open privileged proxy).
    _create_app_with_auth(monkeypatch, obsl_api_key=None)
    assert "obsl_pat_server_key_0123456789abcdef" not in ui_app._API_HEADERS.values()
    assert "X-API-Key" not in ui_app._API_HEADERS


def test_embedded_ui_uses_explicit_obsl_api_key(monkeypatch) -> None:
    _create_app_with_auth(monkeypatch, obsl_api_key="obsl_pat_ui_key_0123456789abcdef")
    assert ui_app._API_HEADERS.get("X-API-Key") == "obsl_pat_ui_key_0123456789abcdef"
