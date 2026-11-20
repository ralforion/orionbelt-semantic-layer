"""No log line or error message carries a full API key (PLAN_authentication.md §9).

Each surface is driven with the right key, a near miss, and no key, with
every logger at DEBUG; neither key may appear anywhere in what was logged
or in what the caller is told. The Flight half lives with the driver, in
``drivers/ob-flight-extension/tests/test_auth.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import AuthConfigError, init_auth, reset_auth
from orionbelt.pgwire.auth import authenticate as pgwire_authenticate
from orionbelt.pgwire.protocol import StartupMessage
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

GOOD_KEY = "obsl_pat_redaction_good_0123456789abcdef"
NEAR_MISS = "obsl_pat_redaction_good_0123456789abcdeX"
SECRETS = (GOOD_KEY, NEAR_MISS)


def _assert_nothing_leaked(caplog: pytest.LogCaptureFixture, *texts: str) -> None:
    logged = [record.getMessage() for record in caplog.records]
    logged += [record.exc_text or "" for record in caplog.records]
    for secret in SECRETS:
        for line in (*logged, *texts):
            assert secret not in line, f"key leaked into: {line[:200]}"


@pytest.fixture
def debug_logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog
    reset_auth()


async def test_rest(debug_logs: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        auth_mode="api_key",
        api_keys=GOOD_KEY,
    )
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode="api_key",
        api_keys=GOOD_KEY,
    )
    bodies: list[str] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for headers in (
                {"X-API-Key": GOOD_KEY},
                {"Authorization": f"Bearer {GOOD_KEY}"},
                {"X-API-Key": NEAR_MISS},
                {"Authorization": f"Bearer {NEAR_MISS}"},
                {},
            ):
                resp = await c.post("/v1/sessions", headers=headers)
                bodies.append(resp.text)
                bodies.extend(str(v) for v in resp.headers.values())
    finally:
        reset_session_manager()
    assert any('"session_id"' in body for body in bodies), "the right key was not accepted"
    _assert_nothing_leaked(debug_logs, *bodies)


def test_pgwire(debug_logs: pytest.LogCaptureFixture) -> None:
    init_auth(auth_mode="api_key", api_keys=GOOD_KEY)
    startup = StartupMessage(protocol_version=196608, parameters={"user": "obsl"})
    results = [
        pgwire_authenticate(startup=startup, password=password)
        for password in (GOOD_KEY, NEAR_MISS, None)
    ]
    assert [r.ok for r in results] == [True, False, False]
    _assert_nothing_leaked(debug_logs, *(repr(r) for r in results))


def test_startup_refusal(debug_logs: pytest.LogCaptureFixture) -> None:
    weak = "obsl_pat_short_1"  # 16 characters: passes the floor, fails the strength check
    with pytest.raises(AuthConfigError) as excinfo:
        init_auth(auth_mode="api_key", api_keys=f"{GOOD_KEY},{weak}")
    message = str(excinfo.value)
    assert weak not in message
    assert weak not in debug_logs.text
    _assert_nothing_leaked(debug_logs, message)
