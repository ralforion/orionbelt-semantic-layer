"""Integration tests for the one-shot batch endpoint and model dedup behavior."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def app():
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        max_age_seconds=settings.session_max_age_seconds,
        max_sessions=settings.max_sessions,
        max_models_per_session=settings.max_models_per_session,
        cleanup_interval=settings.session_cleanup_interval,
    )
    init_session_manager(mgr)
    yield app
    reset_session_manager()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# POST /v1/sessions/{sid}/models — dedup behavior
# ---------------------------------------------------------------------------


class TestModelLoadDedup:
    async def test_default_dedup_reuses_id(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r1 = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        r2 = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        assert r1.status_code == 201
        assert r2.status_code == 201
        d1, d2 = r1.json(), r2.json()
        assert d1["model_id"] == d2["model_id"]
        assert d1["model_load"] == "fresh"
        assert d2["model_load"] == "reused"

    async def test_dedup_false_forces_fresh(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r1 = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        r2 = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML, "dedup": False},
        )
        assert r1.json()["model_id"] != r2.json()["model_id"]
        assert r2.json()["model_load"] == "fresh"

    async def test_dedup_isolated_per_session(self, client: AsyncClient) -> None:
        s1 = (await client.post("/v1/sessions")).json()["session_id"]
        s2 = (await client.post("/v1/sessions")).json()["session_id"]
        r1 = await client.post(f"/v1/sessions/{s1}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        r2 = await client.post(f"/v1/sessions/{s2}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        # Different sessions = different models, even with identical YAML.
        assert r1.json()["model_id"] != r2.json()["model_id"]
        assert r2.json()["model_load"] == "fresh"

    async def test_dedup_after_remove_loads_fresh(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r1 = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        mid = r1.json()["model_id"]
        await client.delete(f"/v1/sessions/{sid}/models/{mid}")
        r2 = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        assert r2.json()["model_id"] != mid
        assert r2.json()["model_load"] == "fresh"


# ---------------------------------------------------------------------------
# POST /v1/oneshot/batch
# ---------------------------------------------------------------------------


_TWO_QUERIES = [
    {
        "id": "q1",
        "query": {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            }
        },
    },
    {
        "id": "q2",
        "query": {"select": {"dimensions": ["Customer Country"], "measures": ["Order Count"]}},
    },
]


class TestOneshotBatchValidation:
    async def test_neither_yaml_nor_id(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={"queries": _TWO_QUERIES},
        )
        assert r.status_code == 422

    async def test_both_yaml_and_id(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "model_id": "abcd1234",
                "queries": _TWO_QUERIES,
            },
        )
        assert r.status_code == 422

    async def test_duplicate_query_ids(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": [
                    {"id": "same", "query": _TWO_QUERIES[0]["query"]},
                    {"id": "same", "query": _TWO_QUERIES[1]["query"]},
                ],
            },
        )
        assert r.status_code == 422

    async def test_empty_queries(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": []},
        )
        assert r.status_code == 422

    async def test_auto_assigned_ids(self, client: AsyncClient) -> None:
        # Omit id on every query — server fills in q0, q1.
        no_id_queries = [
            {"query": _TWO_QUERIES[0]["query"]},
            {"query": _TWO_QUERIES[1]["query"]},
        ]
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": no_id_queries},
        )
        assert r.status_code == 200, r.text
        ids = [res["id"] for res in r.json()["results"]]
        assert ids == ["q0", "q1"]

    async def test_mixed_explicit_and_auto_ids(self, client: AsyncClient) -> None:
        # Mix: explicit id on the second only — first gets auto "q0".
        mixed = [
            {"query": _TWO_QUERIES[0]["query"]},
            {"id": "named", "query": _TWO_QUERIES[1]["query"]},
        ]
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": mixed},
        )
        assert r.status_code == 200
        ids = [res["id"] for res in r.json()["results"]]
        assert ids == ["q0", "named"]

    async def test_explicit_id_collides_with_auto_pattern(self, client: AsyncClient) -> None:
        # Slot 0 auto-assigns "q0"; the explicit "q0" at slot 1 collides.
        clashing = [
            {"query": _TWO_QUERIES[0]["query"]},
            {"id": "q0", "query": _TWO_QUERIES[1]["query"]},
        ]
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": clashing},
        )
        assert r.status_code == 422

    async def test_invalid_yaml(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": "key: [unclosed", "queries": _TWO_QUERIES},
        )
        assert r.status_code == 422


class TestOneshotBatchHappyPath:
    async def test_load_yaml_compile_only(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": _TWO_QUERIES},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "session_id" in data
        assert "model_id" in data
        assert data["model_persisted"] is False  # default persist_model=false
        assert data["model_load"] == "fresh"
        assert len(data["results"]) == 2
        ids = [res["id"] for res in data["results"]]
        assert ids == ["q1", "q2"]
        for res in data["results"]:
            assert res["status"] == "ok"
            assert res["sql"]
            assert res["dialect"]
            assert res["executed"] is False

    async def test_persist_model_keeps_id_addressable(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": _TWO_QUERIES,
                "persist_model": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["model_persisted"] is True
        sid, mid = data["session_id"], data["model_id"]
        # Model should be addressable via the existing endpoints.
        r2 = await client.get(f"/v1/sessions/{sid}/models/{mid}")
        assert r2.status_code == 200

    async def test_persist_false_evicts_model(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": _TWO_QUERIES,
                "persist_model": False,
            },
        )
        data = r.json()
        sid, mid = data["session_id"], data["model_id"]
        r2 = await client.get(f"/v1/sessions/{sid}/models/{mid}")
        assert r2.status_code == 404

    async def test_referenced_model_id(self, client: AsyncClient) -> None:
        # Pre-load a model into a session, then batch against it via model_id.
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "session_id": sid,
                "model_id": mid,
                "queries": _TWO_QUERIES,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == sid
        assert data["model_id"] == mid
        assert data["model_load"] == "referenced"
        assert data["model_persisted"] is True
        # The referenced model is left in place regardless of persist_model.
        r2 = await client.get(f"/v1/sessions/{sid}/models/{mid}")
        assert r2.status_code == 200

    async def test_dedup_in_batch_reuses_loaded_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        # Pre-load identical content with persist=true via the standard endpoint.
        first = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        original_mid = first.json()["model_id"]
        # Batch with same yaml + persist_model=True should reuse, not re-parse.
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "session_id": sid,
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": _TWO_QUERIES,
                "persist_model": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["model_id"] == original_mid
        assert data["model_load"] == "reused"

    async def test_per_query_error_does_not_fail_batch(self, client: AsyncClient) -> None:
        bad_queries = [
            _TWO_QUERIES[0],
            {
                "id": "bad",
                "query": {
                    "select": {
                        "dimensions": ["Nonexistent Dimension"],
                        "measures": ["Total Revenue"],
                    }
                },
            },
        ]
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": bad_queries},
        )
        assert r.status_code == 200
        results = r.json()["results"]
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "error"
        assert results[1]["error"]["code"]

    async def test_fail_fast_cancels_remaining(self, client: AsyncClient) -> None:
        bad_queries = [
            {
                "id": "bad",
                "query": {
                    "select": {
                        "dimensions": ["Nonexistent Dimension"],
                        "measures": ["Total Revenue"],
                    }
                },
            },
            _TWO_QUERIES[0],
            _TWO_QUERIES[1],
        ]
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": bad_queries,
                "fail_fast": True,
            },
        )
        assert r.status_code == 200
        results = r.json()["results"]
        statuses = [res["status"] for res in results]
        assert statuses[0] == "error"
        assert statuses[1] == "cancelled"
        assert statuses[2] == "cancelled"

    async def test_dedup_false_in_batch(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        first = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        original_mid = first.json()["model_id"]
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "session_id": sid,
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": _TWO_QUERIES,
                "dedup": False,
                "persist_model": True,
            },
        )
        data = r.json()
        assert data["model_id"] != original_mid
        assert data["model_load"] == "fresh"


class TestOneshotBatchSettings:
    async def test_settings_exposes_batch_limits(self, client: AsyncClient) -> None:
        r = await client.get("/v1/settings")
        assert r.status_code == 200
        data = r.json()
        assert "oneshot_batch" in data
        cfg = data["oneshot_batch"]
        assert cfg["max_queries"] >= 1
        assert cfg["max_parallelism"] >= 1
        assert cfg["default_timeout_ms"] > 0
        assert cfg["batch_timeout_ms"] > 0

    async def test_max_queries_enforced(self, client: AsyncClient) -> None:
        # Crank in 51 queries — over the default cap of 50.
        many = [{"id": f"q{i}", "query": _TWO_QUERIES[0]["query"]} for i in range(51)]
        r = await client.post(
            "/v1/oneshot/batch",
            json={"model_yaml": SAMPLE_MODEL_YAML, "queries": many},
        )
        assert r.status_code == 422

    async def test_max_parallelism_silently_capped(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "queries": _TWO_QUERIES,
                "max_parallelism": 999,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert any(
            w["code"] == "MAX_PARALLELISM_CAPPED" or "max_parallelism reduced" in w["message"]
            for w in data["batch_warnings"]
        )
