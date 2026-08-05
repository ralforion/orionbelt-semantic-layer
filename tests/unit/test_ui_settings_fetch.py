"""UI ``_fetch_settings`` retry + no-silent-fallback (v2.7.6, issue #89).

Pre-fix: a single transient ``/v1/settings`` failure cached ``{}`` and
the UI silently swapped in the bundled ``examples/orionbelt_1_commerce.yaml``
starter — users saw a different model than what was deployed and had
no way to know why.

Now:
* No cache on settings — every call hits the API.
* 3-attempt retry with backoff covers Cloud Run cold starts.
* Final failure returns ``{"_unreachable": True, "_error": "..."}``;
  the startup branch in ``create_blocks`` honors that flag and shows
  a placeholder explaining the API is down — never falls back to the
  bundled YAML in single-model mode.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

httpx = pytest.importorskip("httpx", reason="httpx required to drive the UI fetch")

from orionbelt.ui import app as ui_app  # noqa: E402


def test_fetch_settings_returns_payload_on_success() -> None:
    fake = {
        "single_model_mode": True,
        "model_yaml": "version: 1.0\nname: deployed_model\n",
        "dialect": {"effective": "duckdb"},
    }

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return fake

    with patch.object(httpx, "get", return_value=_Resp()) as mock_get:
        out = ui_app._fetch_settings("http://example.invalid")
    mock_get.assert_called_once()
    assert out == fake
    assert "_unreachable" not in out


def test_fetch_settings_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloud Run cold-start: first two calls 503, third succeeds."""
    calls = {"n": 0}
    payload = {"single_model_mode": True, "model_yaml": "ok"}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    def flaky_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("simulated cold start")
        return _Resp()

    # Skip the backoff sleep
    monkeypatch.setattr(ui_app.time, "sleep", lambda _s: None)
    monkeypatch.setattr(httpx, "get", flaky_get)
    out = ui_app._fetch_settings("http://example.invalid")
    assert calls["n"] == 3
    assert out == payload


def test_fetch_settings_marks_unreachable_after_all_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt fails — UI must see the unreachable marker, not ``{}``."""
    calls = {"n": 0}

    def always_fail(*args, **kwargs):
        calls["n"] += 1
        raise httpx.ConnectError("permanently down")

    monkeypatch.setattr(ui_app.time, "sleep", lambda _s: None)
    monkeypatch.setattr(httpx, "get", always_fail)
    out = ui_app._fetch_settings("http://example.invalid")
    assert calls["n"] == 3
    assert out.get("_unreachable") is True
    assert "ConnectError" in out.get("_error", "")
    # Crucially: must NOT look like a self-service settings response
    assert "model_yaml" not in out
    assert "single_model_mode" not in out


def test_fetch_settings_no_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-v2.7.6 cached empty results stuck forever — every call now
    hits the API so a recovered API is picked up on next render.
    """
    calls = {"n": 0}

    def fail_then_succeed(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")

        class _R:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"single_model_mode": True, "model_yaml": "y"}

        return _R()

    # First call: fails all 3 retries → unreachable
    monkeypatch.setattr(ui_app.time, "sleep", lambda _s: None)

    # Set up: first call raises 3 times; thereafter succeeds
    state = {"failures_left": 3}

    def gated(*args, **kwargs):
        if state["failures_left"] > 0:
            state["failures_left"] -= 1
            raise httpx.ConnectError("down")

        class _R:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"single_model_mode": True, "model_yaml": "y"}

        return _R()

    monkeypatch.setattr(httpx, "get", gated)
    out1 = ui_app._fetch_settings("http://example.invalid")
    assert out1.get("_unreachable") is True

    out2 = ui_app._fetch_settings("http://example.invalid")
    assert out2.get("_unreachable") is not True
    assert out2["model_yaml"] == "y"


def test_no_cached_settings_module_attr() -> None:
    """The pre-v2.7.6 ``_cached_settings`` global is gone — removing
    the cache was half the fix and the easiest regression to backslide
    on. Trip the test if someone re-introduces it.
    """
    assert not hasattr(ui_app, "_cached_settings"), (
        "_cached_settings was removed in v2.7.6 (#89) — re-introducing it "
        "would re-enable the silent-fallback bug."
    )
