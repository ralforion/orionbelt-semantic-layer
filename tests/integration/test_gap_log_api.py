"""``GET .../gaps``: refused queries over REST, read back by the caller that made them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import init_auth, reset_auth
from orionbelt.service import gap_log
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML

KEY_A = "obsl_pat_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
KEY_B = "obsl_pat_" + "f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1"
TYPO_QUERY = {"select": {"dimensions": ["Customer Country"], "measures": ["Total Revnue"]}}


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    gap_log.configure(gap_log.GAP_NAMES, 100)
    yield
    gap_log.configure(gap_log.GAP_OFF, 1)
    reset_auth()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(settings=Settings(session_ttl_seconds=3600, session_cleanup_interval=9999))
    init_session_manager(SessionManager(ttl_seconds=3600, cleanup_interval=9999))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    reset_session_manager()


async def _session_with_model(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    resp = await client.post("/v1/sessions", headers=headers)
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return sid, resp.json()["model_id"]


async def _refused_query(
    client: AsyncClient, sid: str, mid: str, headers: dict[str, str], query: dict[str, Any]
) -> None:
    resp = await client.post(
        f"/v1/sessions/{sid}/query/sql", json={"model_id": mid, "query": query}, headers=headers
    )
    assert resp.status_code == 422, resp.text


async def _gaps(
    client: AsyncClient, sid: str, mid: str, headers: dict[str, str], **params: Any
) -> dict[str, Any]:
    resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/gaps", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


class TestNoAuth:
    async def test_a_refused_query_is_listed_for_its_session(self, client: AsyncClient) -> None:
        sid, mid = await _session_with_model(client, {})
        await _refused_query(client, sid, mid, {}, TYPO_QUERY)
        await _refused_query(client, sid, mid, {}, TYPO_QUERY)

        body = await _gaps(client, sid, mid, {})
        assert (body["mode"], body["scope"]) == ("names", "own")
        [gap] = body["gaps"]
        assert (gap["code"], gap["subject"], gap["count"]) == ("UNKNOWN_MEASURE", "Total Revnue", 2)
        assert "Total Revenue" in gap["suggestions"]
        event = body["recent"][0]
        assert event["surface"] == "rest"
        assert event["dimensions"] == ["Customer Country"]
        assert event["measures"] == ["Total Revnue"]
        assert event["message"] is None
        assert event["filters"] is None

    async def test_another_session_does_not_see_them(self, client: AsyncClient) -> None:
        sid, mid = await _session_with_model(client, {})
        await _refused_query(client, sid, mid, {}, TYPO_QUERY)
        other_sid, other_mid = await _session_with_model(client, {})
        assert (await _gaps(client, other_sid, other_mid, {}))["gaps"] == []

    async def test_an_obsql_refusal_is_listed(self, client: AsyncClient) -> None:
        sid, mid = await _session_with_model(client, {})
        resp = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={"model_id": mid, "sql": 'SELECT "Bogus Column" FROM m'},
        )
        assert resp.status_code == 400, resp.text
        [gap] = (await _gaps(client, sid, mid, {}))["gaps"]
        assert gap["subject"] == "Bogus Column"

    async def test_limit_and_code_narrow_the_answer(self, client: AsyncClient) -> None:
        sid, mid = await _session_with_model(client, {})
        await _refused_query(client, sid, mid, {}, TYPO_QUERY)
        assert (await _gaps(client, sid, mid, {}, limit=0))["recent"] == []
        assert (await _gaps(client, sid, mid, {}, code="FANOUT"))["gaps"] == []

    async def test_full_mode_also_lists_the_message_and_filters(self, client: AsyncClient) -> None:
        gap_log.configure(gap_log.GAP_FULL, 100)
        sid, mid = await _session_with_model(client, {})
        query = {
            **TYPO_QUERY,
            "where": [{"field": "Customer Country", "op": "equals", "value": "DE"}],
        }
        await _refused_query(client, sid, mid, {}, query)
        event = (await _gaps(client, sid, mid, {}))["recent"][0]
        assert event["message"] == "Unknown measure 'Total Revnue'"
        assert event["filters"] == [{"field": "Customer Country", "op": "equals", "value": "DE"}]

    async def test_off_lists_nothing(self, client: AsyncClient) -> None:
        gap_log.configure(gap_log.GAP_OFF, 1)
        sid, mid = await _session_with_model(client, {})
        await _refused_query(client, sid, mid, {}, TYPO_QUERY)
        body = await _gaps(client, sid, mid, {})
        assert (body["mode"], body["gaps"]) == ("off", [])

    async def test_an_unknown_model_is_a_404(self, client: AsyncClient) -> None:
        sid, _ = await _session_with_model(client, {})
        resp = await client.get(f"/v1/sessions/{sid}/models/nope/gaps")
        assert resp.status_code == 404


class TestApiKeys:
    async def test_each_key_sees_only_its_own_refusals(self, client: AsyncClient) -> None:
        init_auth(auth_mode="api_key", api_keys=f"{KEY_A},{KEY_B}")
        a, b = {"X-API-Key": KEY_A}, {"X-API-Key": KEY_B}
        sid, mid = await _session_with_model(client, a)
        await _refused_query(client, sid, mid, a, TYPO_QUERY)

        assert len((await _gaps(client, sid, mid, a))["gaps"]) == 1
        body = await _gaps(client, sid, mid, b)
        assert (body["scope"], body["gaps"]) == ("own", [])

    async def test_the_shortcut_lists_the_same(self, client: AsyncClient) -> None:
        init_auth(auth_mode="api_key", api_keys=KEY_A)
        a = {"X-API-Key": KEY_A}
        sid, mid = await _session_with_model(client, a)
        await _refused_query(client, sid, mid, a, TYPO_QUERY)

        resp = await client.get("/v1/gaps", headers=a)
        assert resp.status_code == 200, resp.text
        assert resp.json()["model_id"] == mid
        assert resp.json()["gaps"][0]["subject"] == "Total Revnue"
