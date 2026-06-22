"""JSON Schema validation at the REST ingestion boundary.

The model-load and query endpoints validate raw request payloads against
the published JSON Schemas (``obml-schema.json`` / ``query-schema.json``)
before processing, returning 422 on a contract violation. This locks that
behaviour: canonical camelCase payloads pass; snake_case / unknown-key
payloads that Pydantic would otherwise coerce or accept are rejected.
"""

from __future__ import annotations

import json

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


_VALID_MODEL_JSON: dict = {
    "version": 1.0,
    "dataObjects": {
        "Orders": {
            "code": "orders",
            "database": "db",
            "schema": "public",
            "columns": {"Amount": {"code": "amount", "abstractType": "float"}},
        }
    },
    "measures": {
        "Revenue": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
        }
    },
}


def _invalid_model_json() -> dict:
    bad = json.loads(json.dumps(_VALID_MODEL_JSON))
    col = bad["dataObjects"]["Orders"]["columns"]["Amount"]
    col["abstract_type"] = col.pop("abstractType")  # snake_case violates the schema
    return bad


async def test_model_json_snake_case_rejected(client: AsyncClient) -> None:
    sid = await _new_session(client)
    resp = await client.post(
        f"/v1/sessions/{sid}/models", json={"model_json": _invalid_model_json()}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "SCHEMA_VALIDATION"


async def test_model_json_string_form_rejected(client: AsyncClient) -> None:
    sid = await _new_session(client)
    resp = await client.post(
        f"/v1/sessions/{sid}/models",
        json={"model_json": json.dumps(_invalid_model_json())},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "SCHEMA_VALIDATION"


async def test_model_json_valid_loads(client: AsyncClient) -> None:
    sid = await _new_session(client)
    resp = await client.post(f"/v1/sessions/{sid}/models", json={"model_json": _VALID_MODEL_JSON})
    assert resp.status_code == 201, resp.text


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


async def test_shortcut_query_unknown_key_rejected(client: AsyncClient) -> None:
    sid = await _new_session(client)
    await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _VALID_MODEL})
    # Bare QueryObject body (top-level shortcut, auto-resolves the model).
    resp = await client.post(
        "/v1/query/sql",
        json={"select": {"measures": ["Revenue"]}, "bogus": 1},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "SCHEMA_VALIDATION"


async def test_oneshot_batch_query_validated(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/oneshot/batch",
        json={
            "model_yaml": _VALID_MODEL,
            "queries": [{"query": {"select": {"measures": ["Revenue"]}, "bogus": 1}}],
        },
    )
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["code"] == "SCHEMA_VALIDATION"
    assert errors[0]["path"].startswith("queries[0].query")


async def test_oneshot_batch_model_yaml_validated(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/oneshot/batch",
        json={
            "model_yaml": _VALID_MODEL.replace("abstractType:", "abstract_type:"),
            "queries": [{"query": {"select": {"measures": ["Revenue"]}}}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "SCHEMA_VALIDATION"


async def test_validate_endpoint_reports_schema_errors(client: AsyncClient) -> None:
    """The /validate endpoint reports schema violations (not a 422).

    This keeps the UI's Validate button consistent with the schema-guarded
    load/run path: a snake_case model is reported invalid rather than being
    silently coerced and called valid.
    """
    sid = await _new_session(client)
    bad = _VALID_MODEL.replace("abstractType:", "abstract_type:")
    resp = await client.post(f"/v1/sessions/{sid}/validate", json={"model_yaml": bad})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any(e["code"] == "SCHEMA_VALIDATION" for e in body["errors"])


async def test_validate_endpoint_accepts_valid_model(client: AsyncClient) -> None:
    sid = await _new_session(client)
    resp = await client.post(f"/v1/sessions/{sid}/validate", json={"model_yaml": _VALID_MODEL})
    assert resp.status_code == 200, resp.text
    assert resp.json()["valid"] is True


async def test_model_files_preload_validates_at_startup(tmp_path) -> None:
    """A MODEL_FILES entry that violates the schema fails app startup.

    The preload runs in the FastAPI lifespan, so the failure surfaces when
    the lifespan context is entered (i.e. at real server startup).
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text(_VALID_MODEL.replace("abstractType:", "abstract_type:"), encoding="utf-8")
    settings = Settings(
        model_files=str(bad),
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
    )
    app = create_app(settings=settings)
    try:
        # The preload validates each file via ``store.validate`` (schema-aware)
        # and fails startup with a clear message on a contract violation.
        with pytest.raises(ValueError, match="model file validation failed"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        reset_session_manager()


async def test_model_files_preload_accepts_valid_model(tmp_path) -> None:
    """A canonical (camelCase) MODEL_FILES entry preloads cleanly."""
    good = tmp_path / "good.yaml"
    good.write_text(_VALID_MODEL, encoding="utf-8")
    settings = Settings(
        model_files=str(good),
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
    )
    app = create_app(settings=settings)
    try:
        async with app.router.lifespan_context(app):
            pass
    finally:
        reset_session_manager()
