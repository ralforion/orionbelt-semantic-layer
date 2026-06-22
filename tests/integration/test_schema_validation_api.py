"""JSON Schema validation at the REST ingestion boundary.

The model-load and query endpoints validate raw request payloads against
the published JSON Schemas (``obml-schema.json`` / ``query-schema.json``)
before processing, returning 422 on a contract violation. This locks that
behaviour: canonical camelCase payloads pass; snake_case / unknown-key
payloads that Pydantic would otherwise coerce or accept are rejected.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

_VALID_MODEL = """\
version: 1.0
dataObjects:
  Orders:
    code: orders
    database: db
    schema: public
    columns:
      Amount:
        code: amount
        abstractType: float
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    resultType: float
    aggregation: sum
"""


@pytest.fixture
def app():
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    application = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
    )
    init_session_manager(mgr)
    yield application
    reset_session_manager()


@pytest.fixture
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _new_session(client: AsyncClient) -> str:
    resp = await client.post("/v1/sessions")
    return str(resp.json()["session_id"])


async def test_valid_model_loads(client: AsyncClient) -> None:
    sid = await _new_session(client)
    resp = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _VALID_MODEL})
    assert resp.status_code == 201, resp.text


async def test_unknown_top_level_key_rejected(client: AsyncClient) -> None:
    sid = await _new_session(client)
    bad = _VALID_MODEL + "dataObjekts: {}\n"
    resp = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": bad})
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "SCHEMA_VALIDATION"


async def test_snake_case_field_rejected(client: AsyncClient) -> None:
    sid = await _new_session(client)
    # ``abstract_type`` is the snake_case form of the camelCase contract key.
    bad = _VALID_MODEL.replace("abstractType:", "abstract_type:")
    resp = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": bad})
    assert resp.status_code == 422


async def test_valid_query_compiles(client: AsyncClient) -> None:
    sid = await _new_session(client)
    load = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _VALID_MODEL})
    mid = load.json()["model_id"]
    resp = await client.post(
        f"/v1/sessions/{sid}/query/sql",
        json={"model_id": mid, "query": {"select": {"measures": ["Revenue"]}}},
    )
    assert resp.status_code == 200, resp.text


async def test_query_unknown_key_rejected(client: AsyncClient) -> None:
    sid = await _new_session(client)
    load = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _VALID_MODEL})
    mid = load.json()["model_id"]
    resp = await client.post(
        f"/v1/sessions/{sid}/query/sql",
        json={"model_id": mid, "query": {"select": {"measures": ["Revenue"]}, "bogus": 1}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "SCHEMA_VALIDATION"
