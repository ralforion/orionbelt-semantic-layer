"""Integration tests for cache stats + heartbeat endpoints, refresh block parsing."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import (
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.cache.noop import NoopCache
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML

_MODEL_WITH_REFRESH = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    refresh:
      mode: interval
      interval: 1h
  Returns:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Return ID:
        code: ORDER_ID
        abstractType: string
      Refund:
        code: AMOUNT
        abstractType: float
    refresh:
      mode: heartbeat
      maxStaleness: 5m
measures:
  Total Amount:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""


_MODEL_WITH_BAD_REFRESH = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
    refresh:
      mode: interval
      # missing required `interval:` value
"""


@pytest.fixture
def app():
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    fa = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
    )
    init_session_manager(
        mgr,
        cache=NoopCache(),
        cache_config=CacheRuntimeConfig(backend="noop", heartbeat_auth_token="test-token"),
    )
    yield fa
    reset_session_manager()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestCacheStatsEndpoint:
    async def test_default_noop_backend(self, client: AsyncClient) -> None:
        r = await client.get("/v1/cache/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] == "noop"
        assert data["entry_count"] == 0


class TestRefreshBlockParsing:
    async def test_load_with_refresh_block(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_REFRESH}
        )
        assert r.status_code == 201, r.text
        data = r.json()
        # Two dataObjects on the same physical table → consistency-disagreement warning
        codes = [w["code"] for w in data.get("warnings", [])]
        assert "SHARED_TABLE_CONTRACT_DISAGREEMENT" in codes

    async def test_load_with_invalid_refresh_block(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_BAD_REFRESH}
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        codes = [e["code"] for e in detail["errors"]]
        assert "REFRESH_PARSE_ERROR" in codes


class TestHeartbeatEndpoint:
    async def test_heartbeat_requires_token(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/heartbeat",
            json={"database": "WAREHOUSE", "schema": "PUBLIC", "table": "ORDERS"},
        )
        assert r.status_code == 401

    async def test_heartbeat_invalid_token(self, client: AsyncClient) -> None:
        r = await client.post(
            "/v1/heartbeat",
            json={"database": "WAREHOUSE", "schema": "PUBLIC", "table": "ORDERS"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    async def test_heartbeat_valid_token_returns_table_ref(self, client: AsyncClient) -> None:
        # Load a model so affected_data_objects has something to find
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})

        r = await client.post(
            "/v1/heartbeat",
            json={"database": "WAREHOUSE", "schema": "PUBLIC", "table": "ORDERS"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["table_ref"] == "WAREHOUSE.PUBLIC.ORDERS"
        # NoopCache invalidates 0 entries
        assert data["invalidated_cache_entries"] == 0
        # The sample model has Orders pointing to WAREHOUSE.PUBLIC.ORDERS
        assert "Orders" in data["affected_data_objects"]


class TestPhysicalTablesInResponse:
    async def test_compile_response_carries_physical_tables(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        r = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    },
                },
                "dialect": "postgres",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert "physical_tables" in data
        # SAMPLE_MODEL_YAML maps Customers and Orders to distinct physical tables
        assert any("ORDERS" in t for t in data["physical_tables"])
        assert any("CUSTOMERS" in t for t in data["physical_tables"])
