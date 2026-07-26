"""Integration tests for the session-scoped OSI model endpoints.

Covers ``POST /v1/sessions/{id}/models/from-osi`` (load_model_from_osi)
and ``GET /v1/sessions/{id}/models/{mid}/osi`` (export_model_to_osi),
including a load -> export round trip and the error paths.
"""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings


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


# A minimal, valid OSI v0.2 model: one dataset, one metric.
_OSI_V02_MINIMAL = {
    "version": "0.2.0.dev0",
    "semantic_model": [
        {
            "name": "demo",
            "datasets": [
                {
                    "name": "Orders",
                    "source": "WAREHOUSE.PUBLIC.orders",
                    "primary_key": ["order_id"],
                    "fields": [
                        {
                            "name": "order_id",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "order_id"}]
                            },
                        },
                        {
                            "name": "amount",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "amount"}]
                            },
                        },
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "total_revenue",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(Orders.amount)"}]
                    },
                }
            ],
        }
    ],
}


async def _new_session(client: AsyncClient) -> str:
    resp = await client.post("/v1/sessions")
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


class TestLoadModelFromOsi:
    async def test_loads_osi_into_store(self, client: AsyncClient) -> None:
        sid = await _new_session(client)
        resp = await client.post(
            f"/v1/sessions/{sid}/models/from-osi",
            json={"osi_yaml": yaml.safe_dump(_OSI_V02_MINIMAL)},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["model_id"]
        assert body["data_objects"] == 1
        assert body["measures"] >= 1
        # OSI-specific extras present
        assert "conversion_warnings" in body
        assert body["input_validation"] is not None
        assert body["input_validation"]["schema_valid"] is True

        # The model is now discoverable via the normal model listing.
        listing = await client.get(f"/v1/sessions/{sid}/models")
        assert listing.status_code == 200
        assert any(m["model_id"] == body["model_id"] for m in listing.json())

    async def test_invalid_yaml_returns_400(self, client: AsyncClient) -> None:
        sid = await _new_session(client)
        resp = await client.post(
            f"/v1/sessions/{sid}/models/from-osi",
            json={"osi_yaml": "key: [unclosed"},
        )
        assert resp.status_code == 400

    async def test_unknown_session_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/v1/sessions/does-not-exist/models/from-osi",
            json={"osi_yaml": yaml.safe_dump(_OSI_V02_MINIMAL)},
        )
        assert resp.status_code == 404


class TestExportModelToOsi:
    async def test_round_trip_load_then_export(self, client: AsyncClient) -> None:
        sid = await _new_session(client)
        load = await client.post(
            f"/v1/sessions/{sid}/models/from-osi",
            json={"osi_yaml": yaml.safe_dump(_OSI_V02_MINIMAL)},
        )
        assert load.status_code == 201, load.text
        model_id = load.json()["model_id"]

        export = await client.get(
            f"/v1/sessions/{sid}/models/{model_id}/osi",
            params={"model_name": "demo"},
        )
        assert export.status_code == 200, export.text
        body = export.json()
        assert body["output_yaml"]
        # The exported text parses as YAML and looks like an OSI document.
        osi = yaml.safe_load(body["output_yaml"])
        assert isinstance(osi, dict)
        assert "semantic_model" in osi
        assert body["validation"]["schema_valid"] is True

    async def test_export_unknown_model_returns_404(self, client: AsyncClient) -> None:
        sid = await _new_session(client)
        resp = await client.get(f"/v1/sessions/{sid}/models/nope/osi")
        assert resp.status_code == 404

    async def test_export_unknown_session_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/v1/sessions/does-not-exist/models/m1/osi")
        assert resp.status_code == 404
