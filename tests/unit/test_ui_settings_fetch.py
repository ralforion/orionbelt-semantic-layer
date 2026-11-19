"""UI ``_fetch_settings`` retry + no-silent-fallback (v2.7.6, issue #89).

Pre-fix: a single transient ``/v1/settings`` failure cached ``{}`` and
the UI silently swapped in the bundled ``examples/orionbelt_1_commerce.yaml``
starter — users saw a different model than what was deployed and had
no way to know why.

Now:
* No cache on settings — every call hits the API.
* 3-attempt retry with backoff; at startup the UI keeps trying for up to
  ``_STARTUP_API_WAIT`` seconds while the API is still starting.
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


# The UI's startup waits for an API that is still starting (2026-11-19: the
# demo API's cold start took 95 s, the UI gave up after about 20 s, and every
# page showed "API unreachable" until the UI restarted).


class _Clock:
    """Stands in for ``api_client.time``: a request or a sleep moves it on."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://example.invalid/v1/settings")
    return httpx.HTTPStatusError(
        str(code), request=request, response=httpx.Response(code, request=request)
    )


class _Settings:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"single_model_mode": True, "model_yaml": "y"}


def _api(clock: _Clock, failures: list[Exception]) -> tuple[dict, object]:
    """``httpx.get`` failing with *failures* in turn, then answering."""
    calls = {"n": 0}

    def get(*args, **kwargs):
        calls["n"] += 1
        if failures:
            exc = failures.pop(0)
            if isinstance(exc, httpx.TimeoutException):
                clock.now += kwargs["timeout"]
            raise exc
        return _Settings()

    return calls, get


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    from orionbelt.ui import api_client

    fake = _Clock()
    monkeypatch.setattr(api_client, "time", fake)
    return fake


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("cold start"), _status_error(503), _status_error(429)],
    ids=["timeout", "503", "429"],
)
def test_startup_waits_for_an_api_that_is_starting(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    calls, get = _api(clock, [failure] * 8)
    monkeypatch.setattr(httpx, "get", get)
    out = ui_app._fetch_settings("http://example.invalid", wait=ui_app._STARTUP_API_WAIT)
    assert out == {"single_model_mode": True, "model_yaml": "y"}
    assert calls["n"] == 9


def test_the_startup_wait_ends(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, get = _api(clock, [httpx.ReadTimeout("never ready")] * 1000)
    monkeypatch.setattr(httpx, "get", get)
    out = ui_app._fetch_settings("http://example.invalid", wait=ui_app._STARTUP_API_WAIT)
    assert out.get("_unreachable") is True
    assert "ReadTimeout" in out["_error"]
    assert calls["n"] > 3
    # The last try starts before the deadline and may take its 5 s timeout.
    assert clock.now <= ui_app._STARTUP_API_WAIT + 5


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), _status_error(404), _status_error(401)],
    ids=["refused", "404", "401"],
)
def test_startup_does_not_wait_for_an_api_that_is_not_starting(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Nothing listens, or the API answers and refuses: waiting would only
    delay a UI started without its API."""
    calls, get = _api(clock, [failure] * 1000)
    monkeypatch.setattr(httpx, "get", get)
    out = ui_app._fetch_settings("http://example.invalid", wait=ui_app._STARTUP_API_WAIT)
    assert out.get("_unreachable") is True
    assert calls["n"] == 3


def test_a_page_load_does_not_wait(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, get = _api(clock, [httpx.ReadTimeout("cold start")] * 1000)
    monkeypatch.setattr(httpx, "get", get)
    out = ui_app._fetch_settings("http://example.invalid")
    assert out.get("_unreachable") is True
    assert calls["n"] == 3


def test_the_ui_waits_at_startup() -> None:
    with patch.object(ui_app, "_fetch_settings", return_value={"_unreachable": True}) as fetch:
        ui_app.create_blocks(default_api_url="http://example.invalid")
    fetch.assert_called_once_with("http://example.invalid", wait=ui_app._STARTUP_API_WAIT)
