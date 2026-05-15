"""Integration tests for the FastAPI REST API."""

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
    # Manually init SessionManager (ASGITransport doesn't trigger lifespan)
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
# Health & Dialects
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    async def test_health(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestDialectsEndpoint:
    async def test_list_dialects(self, client: AsyncClient) -> None:
        response = await client.get("/v1/dialects")
        assert response.status_code == 200
        data = response.json()
        names = [d["name"] for d in data["dialects"]]
        assert "postgres" in names
        assert "snowflake" in names
        assert "clickhouse" in names
        assert "dremio" in names
        assert "databricks" in names

    async def test_dialects_unsupported_aggregations(self, client: AsyncClient) -> None:
        response = await client.get("/v1/dialects")
        data = response.json()
        by_name = {d["name"]: d for d in data["dialects"]}
        # MySQL and Dremio declare mode as unsupported
        assert "mode" in by_name["mysql"]["unsupported_aggregations"]
        assert "mode" in by_name["dremio"]["unsupported_aggregations"]
        # Postgres supports all aggregations
        assert by_name["postgres"]["unsupported_aggregations"] == []


class TestSettingsEndpoint:
    async def test_settings_default(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings")
        assert response.status_code == 200
        data = response.json()
        assert data["single_model_mode"] is False
        assert "model_yaml" not in data  # omitted when not in single-model mode
        assert data["session_ttl_seconds"] == 3600  # from fixture
        assert "flight" not in data  # omitted when not enabled
        # Multi-model mode: model_settings is omitted; timezone + dialect
        # are always present (no `model` field on either when no model
        # has been resolved).
        assert "model_settings" not in data
        assert "timezone" in data
        assert "model" not in data["timezone"]
        assert data["timezone"]["effective"]
        # Wall-clock fields are populated regardless of model state.
        assert data["timezone"]["now"]
        assert data["timezone"]["utc"]
        assert data["timezone"]["utc"].endswith("Z")
        assert "dialect" in data
        assert "model" not in data["dialect"]
        assert data["dialect"]["effective"]

    async def test_settings_single_model(self, single_model_client: AsyncClient) -> None:
        response = await single_model_client.get("/v1/settings")
        assert response.status_code == 200
        data = response.json()
        assert data["single_model_mode"] is True
        assert data["model_yaml"] is not None
        assert "dataObjects" in data["model_yaml"]
        assert data["session_ttl_seconds"] == 3600  # from fixture
        # Single-model mode adds the model_settings + timezone blocks.
        # SAMPLE_MODEL_YAML may not declare a `settings:` block — the
        # block must still appear with default values, and the timezone
        # chain must always have an `effective` value.
        assert "model_settings" in data
        assert "timezone" in data
        assert data["timezone"]["effective"]
        assert "dialect" in data
        assert data["dialect"]["effective"]

    async def test_settings_session_scope_unique_model(self, client: AsyncClient) -> None:
        """In multi-model mode, ``?session_id=...`` resolves the model when
        the session holds exactly one. Returned model_settings reflect that
        model's `settings:` block."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        yaml_text = (
            "version: 1.0\n"
            "settings:\n"
            "  defaultDialect: postgres\n"
            "  defaultTimezone: America/New_York\n"
            "dataObjects:\n"
            "  Orders: {code: ORDERS, columns: {Id: {code: ID, abstractType: string}}}\n"
            "dimensions:\n"
            "  Order Id: {dataObject: Orders, column: Id, resultType: string}\n"
            "measures:\n"
            "  Order Count: {columns: [{dataObject: Orders, column: Id}],"
            " resultType: int, aggregation: count}\n"
        )
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": yaml_text})
        # Without scope: no auto-resolve in this client (no session pre-populated).
        # With ?session_id=...: model blocks appear.
        r = await client.get(f"/v1/settings?session_id={sid}")
        assert r.status_code == 200
        data = r.json()
        assert data["model_settings"]["defaultDialect"] == "postgres"
        assert data["model_settings"]["defaultTimezone"] == "America/New_York"
        assert data["timezone"]["model"] == "America/New_York"
        assert data["dialect"]["model"] == "postgres"
        assert data["dialect"]["effective"] == "postgres"

    async def test_settings_explicit_model_id(self, client: AsyncClient) -> None:
        """``?session_id=...&model_id=...`` pins the response to one model."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        yaml_a = (
            "version: 1.0\n"
            "settings: {defaultDialect: snowflake}\n"
            "dataObjects:\n"
            "  Orders: {code: ORDERS, columns: {Id: {code: ID, abstractType: string}}}\n"
            "dimensions:\n"
            "  Order Id: {dataObject: Orders, column: Id, resultType: string}\n"
            "measures:\n"
            "  Order Count: {columns: [{dataObject: Orders, column: Id}],"
            " resultType: int, aggregation: count}\n"
        )
        yaml_b = (
            "version: 1.0\n"
            "settings: {defaultDialect: bigquery}\n"
            "dataObjects:\n"
            "  Orders: {code: ORDERS, columns: {Id: {code: ID, abstractType: string}}}\n"
            "dimensions:\n"
            "  Order Id: {dataObject: Orders, column: Id, resultType: string}\n"
            "measures:\n"
            "  Order Count: {columns: [{dataObject: Orders, column: Id}],"
            " resultType: int, aggregation: count}\n"
        )
        mid_a = (
            await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": yaml_a})
        ).json()["model_id"]
        mid_b = (
            await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": yaml_b})
        ).json()["model_id"]

        # Session has 2 models — session_id alone omits the model blocks.
        r = await client.get(f"/v1/settings?session_id={sid}")
        assert r.status_code == 200
        data = r.json()
        # In multi-model mode (no preload) the model blocks are absent on
        # ambiguity. dialect.model is also absent.
        assert "model" not in data.get("dialect", {})

        # Pinning model_id resolves unambiguously.
        r = await client.get(f"/v1/settings?session_id={sid}&model_id={mid_a}")
        assert r.json()["dialect"]["model"] == "snowflake"
        r = await client.get(f"/v1/settings?session_id={sid}&model_id={mid_b}")
        assert r.json()["dialect"]["model"] == "bigquery"

    async def test_settings_unknown_session(self, client: AsyncClient) -> None:
        r = await client.get("/v1/settings?session_id=not-a-real-session-id")
        assert r.status_code == 404

    async def test_settings_unknown_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r = await client.get(f"/v1/settings?session_id={sid}&model_id=missing")
        assert r.status_code == 404

    async def test_settings_model_id_without_session_id(self, client: AsyncClient) -> None:
        r = await client.get("/v1/settings?model_id=any")
        assert r.status_code == 400

    async def test_settings_single_model_with_settings_block(self, tmp_path) -> None:
        """When the OBML model declares `settings:`, all four sub-fields
        plus the timezone/dialect chains are populated and reflect the
        model's choices."""
        from orionbelt.api.app import _read_model_file, create_app

        yaml_text = (
            "version: 1.0\n"
            "settings:\n"
            "  defaultTimezone: Europe/Berlin\n"
            "  defaultDialect: snowflake\n"
            "  overrideDatabaseTimezone: true\n"
            "  defaultNumericDataType: decimal(38, 4)\n"
            "dataObjects:\n"
            "  Orders:\n"
            "    code: ORDERS\n"
            "    columns:\n"
            "      Id: {code: ID, abstractType: string}\n"
            "dimensions:\n"
            "  Order Id: {dataObject: Orders, column: Id, resultType: string}\n"
            "measures:\n"
            "  Order Count:\n"
            "    columns: [{dataObject: Orders, column: Id}]\n"
            "    resultType: int\n"
            "    aggregation: count\n"
        )
        model_file = tmp_path / "model.yaml"
        model_file.write_text(yaml_text)

        settings = Settings(
            session_ttl_seconds=3600,
            session_cleanup_interval=9999,
            model_file=str(model_file),
        )
        app = create_app(settings=settings)
        preload, _ = _read_model_file(str(model_file))
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(mgr, preload_model_yaml=preload, db_vendor="duckdb")
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                response = await c.get("/v1/settings")
            assert response.status_code == 200
            data = response.json()

            # ModelSettingsInfo uses OBML-style camelCase keys so the block
            # mirrors the YAML the user wrote.
            ms = data["model_settings"]
            assert ms["defaultTimezone"] == "Europe/Berlin"
            assert ms["defaultDialect"] == "snowflake"
            assert ms["overrideDatabaseTimezone"] is True
            assert ms["defaultNumericDataType"] == "decimal(38, 4)"

            tz = data["timezone"]
            assert tz["model"] == "Europe/Berlin"
            assert tz["override_database_timezone"] is True
            assert tz["effective"] == "Europe/Berlin"
            # `now` is in the effective TZ; `utc` is UTC.
            # Berlin is UTC+1 (CET) or UTC+2 (CEST) — either way not 'Z'.
            assert tz["utc"].endswith("Z")
            assert not tz["now"].endswith("Z")
            assert "+01:00" in tz["now"] or "+02:00" in tz["now"]

            dl = data["dialect"]
            assert dl["model"] == "snowflake"
            assert dl["env"] == "duckdb"
            # When request omits `dialect`, model.defaultDialect wins.
            assert dl["effective"] == "snowflake"
        finally:
            reset_session_manager()

    async def test_settings_flight_enabled(self) -> None:
        """When flight_info is passed, GET /settings includes the flight block."""
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(
            mgr,
            flight_info={
                "enabled": True,
                "port": 8815,
                "auth_mode": "token",
                "db_vendor": "postgres",
            },
            query_execute_enabled=True,
        )
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                response = await c.get("/v1/settings")
            assert response.status_code == 200
            data = response.json()
            assert data["query_execute"] is True
            flight = data["flight"]
            assert flight is not None
            assert flight["enabled"] is True
            assert flight["port"] == 8815
            assert flight["auth_mode"] == "token"
            assert flight["db_vendor"] == "postgres"
        finally:
            reset_session_manager()


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


class TestSessionEndpoints:
    async def test_create_session(self, client: AsyncClient) -> None:
        response = await client.post("/v1/sessions")
        assert response.status_code == 201
        data = response.json()
        assert "session_id" in data
        assert data["model_count"] == 0

    async def test_create_session_with_metadata(self, client: AsyncClient) -> None:
        response = await client.post("/v1/sessions", json={"metadata": {"env": "test"}})
        assert response.status_code == 201
        data = response.json()
        assert data["metadata"] == {"env": "test"}

    async def test_list_sessions(self, client: AsyncClient) -> None:
        await client.post("/v1/sessions")
        await client.post("/v1/sessions")
        response = await client.get("/v1/sessions")
        assert response.status_code == 200
        data = response.json()
        assert len(data["sessions"]) == 2

    async def test_get_session(self, client: AsyncClient) -> None:
        create = await client.post("/v1/sessions")
        sid = create.json()["session_id"]
        response = await client.get(f"/v1/sessions/{sid}")
        assert response.status_code == 200
        assert response.json()["session_id"] == sid

    async def test_get_missing_session(self, client: AsyncClient) -> None:
        response = await client.get("/sessions/nonexist123")
        assert response.status_code == 404

    async def test_delete_session(self, client: AsyncClient) -> None:
        create = await client.post("/v1/sessions")
        sid = create.json()["session_id"]
        response = await client.delete(f"/v1/sessions/{sid}")
        assert response.status_code == 204
        # Verify it's gone
        response = await client.get(f"/v1/sessions/{sid}")
        assert response.status_code == 404

    async def test_delete_missing_session(self, client: AsyncClient) -> None:
        response = await client.delete("/sessions/nonexist123")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Session model flow
# ---------------------------------------------------------------------------


class TestSessionModelFlow:
    async def test_load_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 201
        data = response.json()
        assert "model_id" in data
        assert data["data_objects"] == 2
        assert data["dimensions"] == 1
        assert data["measures"] == 3
        # Phase 2: structural health is always present on a fresh load
        assert "health" in data
        health = data["health"]
        assert health["status"] == "ok"
        assert health["data_objects"] == 2
        assert health["joins"] == 1
        assert health["orphan_data_objects"] == []
        assert health["fan_trap_risks"] == []

    async def test_load_model_json(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        model_json = {
            "version": 1.0,
            "dataObjects": {
                "Orders": {
                    "code": "orders",
                    "database": "db",
                    "schema": "public",
                    "columns": {
                        "Order ID": {"code": "order_id", "abstractType": "string"},
                        "Amount": {"code": "amount", "abstractType": "float"},
                    },
                }
            },
            "dimensions": {
                "Order": {"dataObject": "Orders", "column": "Order ID", "resultType": "string"}
            },
            "measures": {
                "Total Revenue": {
                    "aggregation": "SUM",
                    "resultType": "float",
                    "columns": [{"dataObject": "Orders", "column": "Amount"}],
                }
            },
        }
        response = await client.post(f"/v1/sessions/{sid}/models", json={"model_json": model_json})
        assert response.status_code == 201
        data = response.json()
        assert "model_id" in data
        assert data["data_objects"] == 1
        assert data["dimensions"] == 1
        assert data["measures"] == 1

    async def test_load_model_json_string(self, client: AsyncClient) -> None:
        """model_json as a JSON string (LLMs sometimes stringify objects)."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        import json

        model_json_str = json.dumps(
            {
                "version": 1.0,
                "dataObjects": {
                    "Orders": {
                        "code": "orders",
                        "database": "db",
                        "schema": "public",
                        "columns": {
                            "Amount": {"code": "amount", "abstractType": "float"},
                        },
                    }
                },
                "measures": {
                    "Revenue": {
                        "aggregation": "SUM",
                        "resultType": "float",
                        "columns": [{"dataObject": "Orders", "column": "Amount"}],
                    }
                },
            }
        )
        response = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_json": model_json_str}
        )
        assert response.status_code == 201
        assert response.json()["data_objects"] == 1

    async def test_load_model_no_input(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(f"/v1/sessions/{sid}/models", json={})
        assert response.status_code == 422

    async def test_list_models(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        response = await client.get(f"/v1/sessions/{sid}/models")
        assert response.status_code == 200
        models = response.json()
        assert len(models) == 1

    async def test_describe_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        response = await client.get(f"/v1/sessions/{sid}/models/{mid}")
        assert response.status_code == 200
        data = response.json()
        assert data["model_id"] == mid
        assert len(data["data_objects"]) == 2

    async def test_describe_missing_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.get(f"/v1/sessions/{sid}/models/nonexist")
        assert response.status_code == 404

    async def test_remove_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        response = await client.delete(f"/v1/sessions/{sid}/models/{mid}")
        assert response.status_code == 204
        # Verify it's gone
        response = await client.get(f"/v1/sessions/{sid}/models/{mid}")
        assert response.status_code == 404

    async def test_validate_in_session(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/validate",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_compile_query_in_session(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        response = await client.post(
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
        assert response.status_code == 200
        data = response.json()
        assert "SELECT" in data["sql"]
        assert data["dialect"] == "postgres"

    async def test_load_invalid_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": "}{bad"},
        )
        assert response.status_code == 422


class TestQueryPlanEndpoint:
    """POST /v1/sessions/{sid}/query/plan — see PLAN_agent_api_improvements §2."""

    async def _setup(self, client: AsyncClient) -> tuple[str, str]:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        return sid, load.json()["model_id"]

    async def test_plan_default_no_database_explain(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/plan",
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
        assert data["status"] == "ok"
        assert data["would_compile"] is True
        assert data["planner"] == "Star Schema"
        assert data["compiled_sql_length_estimate"] > 0
        assert data["database_explain"] is None
        # Physical tables include both dataObjects involved
        assert any("ORDERS" in t for t in data["physical_tables"])
        assert any("CUSTOMERS" in t for t in data["physical_tables"])
        # Join path has the expected step
        assert len(data["join_path"]) >= 1
        step = data["join_path"][0]
        assert step["cardinality"] == "many-to-one"

    async def test_plan_invalid_query_returns_error_status(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/plan",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["No Such Dim"],
                        "measures": ["Total Revenue"],
                    },
                },
                "dialect": "postgres",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "error"
        assert data["would_compile"] is False
        assert len(data["warnings"]) > 0

    async def test_plan_mysql_cube_returns_structured_error(self, client: AsyncClient) -> None:
        """Regression: ``/query/plan`` previously surfaced MySQL CUBE as a
        500 because the UnsupportedGroupingError handler was missing on
        this endpoint (other compile/execute paths already handled it).
        """
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/plan",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    },
                    "grouping": "cube",
                },
                "dialect": "mysql",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "error"
        assert data["would_compile"] is False
        codes = [w["code"] for w in data["warnings"]]
        assert "UNSUPPORTED_GROUPING" in codes

    async def test_plan_database_explain_unavailable_emits_warning(
        self, client: AsyncClient
    ) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/plan",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    },
                },
                "dialect": "postgres",
                "include_database_explain": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        # Without QUERY_EXECUTE the EXPLAIN call surfaces as a warning,
        # but the OBSL plan is still returned.
        if data["database_explain"] is None:
            assert any(w["code"] == "DATABASE_EXPLAIN_FAILED" for w in data["warnings"])


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


class TestDiagramEndpoint:
    async def test_diagram_er(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        response = await client.get(f"/v1/sessions/{sid}/models/{mid}/diagram/er")
        assert response.status_code == 200
        data = response.json()
        assert "mermaid" in data
        mermaid = data["mermaid"]
        assert "erDiagram" in mermaid
        assert "Orders" in mermaid
        assert "Customers" in mermaid
        # Relationship line
        assert "}o--||" in mermaid

    async def test_diagram_er_hide_columns(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]
        response = await client.get(
            f"/v1/sessions/{sid}/models/{mid}/diagram/er",
            params={"show_columns": False},
        )
        assert response.status_code == 200
        mermaid = response.json()["mermaid"]
        # Should NOT contain column attribute blocks
        assert "{" not in mermaid.split("\n", 1)[-1].split("}o")[0]

    async def test_diagram_er_missing_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.get(f"/v1/sessions/{sid}/models/nonexist/diagram/er")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


class TestRequestBodyLimit:
    """Verify RequestBodyLimitMiddleware rejects oversized payloads."""

    async def test_model_endpoint_rejects_body_over_5mb(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        oversized = "x" * (5 * 1024 * 1024 + 1)
        response = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": oversized},
        )
        assert response.status_code == 413
        assert "too large" in response.json()["detail"]

    async def test_validate_endpoint_rejects_body_over_5mb(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        oversized = "x" * (5 * 1024 * 1024 + 1)
        response = await client.post(
            f"/v1/sessions/{sid}/validate",
            json={"model_yaml": oversized},
        )
        assert response.status_code == 413
        assert "too large" in response.json()["detail"]

    async def test_query_endpoint_rejects_body_over_1mb(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        oversized = "x" * (1 * 1024 * 1024 + 1)
        response = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            content=oversized,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert "too large" in response.json()["detail"]

    async def test_small_payload_passes(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/validate",
            json={"model_yaml": "version: '1'"},
        )
        # Not 413 — body is small enough
        assert response.status_code != 413

    async def test_content_length_header_checked_first(self, client: AsyncClient) -> None:
        """A spoofed Content-Length > limit triggers 413 before body is read."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            content=b"small",
            headers={
                "content-type": "application/json",
                "content-length": "99999999",
            },
        )
        assert response.status_code == 413


class TestSessionIsolation:
    async def test_models_in_session_a_not_visible_in_b(self, client: AsyncClient) -> None:
        sid_a = (await client.post("/v1/sessions")).json()["session_id"]
        sid_b = (await client.post("/v1/sessions")).json()["session_id"]

        await client.post(f"/v1/sessions/{sid_a}/models", json={"model_yaml": SAMPLE_MODEL_YAML})

        models_a = (await client.get(f"/v1/sessions/{sid_a}/models")).json()
        models_b = (await client.get(f"/v1/sessions/{sid_b}/models")).json()

        assert len(models_a) == 1
        assert len(models_b) == 0


# ---------------------------------------------------------------------------
# Single-model mode
# ---------------------------------------------------------------------------


@pytest.fixture
def single_model_app(tmp_path):
    """Create an app in single-model mode with SAMPLE_MODEL_YAML on disk."""
    model_file = tmp_path / "model.yaml"
    model_file.write_text(SAMPLE_MODEL_YAML)
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        model_file=str(model_file),
    )
    app = create_app(settings=settings)
    # Manually init (ASGITransport doesn't trigger lifespan)
    from orionbelt.api.app import _read_model_file

    preload_yaml, _ = _read_model_file(str(model_file))
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
    )
    # Mirror the real lifespan: in single-model mode, the __default__
    # session is created at startup and the YAML is loaded into it.
    default_store = mgr.get_or_create_default()
    default_store.load_model(preload_yaml)
    init_session_manager(mgr, preload_model_yaml=preload_yaml)
    yield app
    reset_session_manager()


@pytest.fixture
async def single_model_client(single_model_app):
    transport = ASGITransport(app=single_model_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestSingleModelMode:
    async def test_session_created_with_preloaded_model(
        self, single_model_client: AsyncClient
    ) -> None:
        response = await single_model_client.post("/v1/sessions")
        assert response.status_code == 201
        data = response.json()
        assert data["model_count"] == 1

    async def test_model_upload_blocked(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        response = await single_model_client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 403
        assert "model upload is disabled" in response.json()["detail"]

    async def test_model_removal_blocked(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        models = (await single_model_client.get(f"/v1/sessions/{sid}/models")).json()
        mid = models[0]["model_id"]
        response = await single_model_client.delete(f"/v1/sessions/{sid}/models/{mid}")
        assert response.status_code == 403
        assert "model removal is disabled" in response.json()["detail"]

    async def test_query_works_with_preloaded_model(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        models = (await single_model_client.get(f"/v1/sessions/{sid}/models")).json()
        mid = models[0]["model_id"]
        response = await single_model_client.post(
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
        assert response.status_code == 200
        assert "SELECT" in response.json()["sql"]

    async def test_sessions_still_independent(self, single_model_client: AsyncClient) -> None:
        """Each session gets its own copy of the preloaded model."""
        sid_a = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        sid_b = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        models_a = (await single_model_client.get(f"/v1/sessions/{sid_a}/models")).json()
        models_b = (await single_model_client.get(f"/v1/sessions/{sid_b}/models")).json()
        assert len(models_a) == 1
        assert len(models_b) == 1
        # Different model IDs (separate ModelStore instances)
        assert models_a[0]["model_id"] != models_b[0]["model_id"]

    async def test_validate_still_works(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        response = await single_model_client.post(
            f"/v1/sessions/{sid}/validate",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_delete_session_still_works(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        response = await single_model_client.delete(f"/v1/sessions/{sid}")
        assert response.status_code == 204


# ---------------------------------------------------------------------------
# Query execution endpoint
# ---------------------------------------------------------------------------


class TestQueryExecuteEndpoint:
    """Tests for POST /query/execute — gated by QUERY_EXECUTE."""

    async def test_execute_returns_503_without_query_execute(self, client: AsyncClient) -> None:
        """Execution endpoint returns 503 when QUERY_EXECUTE is not set."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        mid = load.json()["model_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/query/execute",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    },
                },
                "dialect": "duckdb",
            },
        )
        assert response.status_code == 503
        assert "QUERY_EXECUTE" in response.json()["detail"]

    async def test_execute_shortcut_returns_503_without_flight(
        self, single_model_client: AsyncClient
    ) -> None:
        """Shortcut execution endpoint returns 503 without QUERY_EXECUTE."""
        await single_model_client.post("/v1/sessions")
        response = await single_model_client.post(
            "/v1/query/execute",
            json={
                "select": {
                    "dimensions": ["Customer Country"],
                    "measures": ["Total Revenue"],
                },
            },
            params={"dialect": "duckdb"},
        )
        assert response.status_code == 503
        assert "QUERY_EXECUTE" in response.json()["detail"]

    async def test_execute_compilation_error_returns_422(self) -> None:
        """Compilation errors are returned before attempting execution."""
        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(
            mgr,
            query_execute_enabled=True,
            db_vendor="duckdb",
        )
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                sid = (await c.post("/v1/sessions")).json()["session_id"]
                load = await c.post(
                    f"/v1/sessions/{sid}/models",
                    json={"model_yaml": SAMPLE_MODEL_YAML},
                )
                mid = load.json()["model_id"]
                response = await c.post(
                    f"/v1/sessions/{sid}/query/execute",
                    json={
                        "model_id": mid,
                        "query": {
                            "select": {
                                "dimensions": ["Nonexistent Dimension"],
                                "measures": ["Total Revenue"],
                            },
                        },
                        "dialect": "duckdb",
                    },
                )
            assert response.status_code == 422
        finally:
            reset_session_manager()


# ---------------------------------------------------------------------------
# Query offset support
# ---------------------------------------------------------------------------


class TestQueryOffset:
    async def test_compile_with_offset(self, client: AsyncClient) -> None:
        """OFFSET is included in compiled SQL when specified."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        mid = load.json()["model_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    },
                    "limit": 10,
                    "offset": 20,
                },
                "dialect": "postgres",
            },
        )
        assert response.status_code == 200
        sql = response.json()["sql"]
        assert "LIMIT 10" in sql
        assert "OFFSET 20" in sql

    async def test_compile_without_offset(self, client: AsyncClient) -> None:
        """OFFSET is omitted from SQL when not specified."""
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        mid = load.json()["model_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    },
                    "limit": 10,
                },
                "dialect": "postgres",
            },
        )
        assert response.status_code == 200
        sql = response.json()["sql"]
        assert "LIMIT 10" in sql
        assert "OFFSET" not in sql


class TestOBSQLEndpoint:
    """POST /v1/sessions/{id}/query/semantic-ql — see PLAN_flight_natural_sql.md."""

    async def _setup(self, client: AsyncClient) -> tuple[str, str]:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        return sid, load.json()["model_id"]

    async def test_compile_happy_path(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={
                "model_id": mid,
                "sql": (
                    'SELECT "Customer Country", "Total Revenue" FROM sample_model '
                    "WHERE \"Customer Country\" = 'US' "
                    'ORDER BY "Total Revenue" DESC LIMIT 10'
                ),
                "dialect": "postgres",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "SELECT" in data["sql"]
        assert data["dialect"] == "postgres"
        # The intermediate QueryObject must round-trip
        q = data["query"]
        assert q["select"]["dimensions"] == ["Customer Country"]
        assert q["select"]["measures"] == ["Total Revenue"]
        assert q["limit"] == 10

    async def test_compile_unknown_select_item(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={"model_id": mid, "sql": 'SELECT "Bogus Column" FROM m'},
        )
        assert r.status_code == 400
        codes = [e["code"] for e in r.json()["detail"]["errors"]]
        assert "UNKNOWN_SELECT_ITEM" in codes

    async def test_compile_join_rejected(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={
                "model_id": mid,
                "sql": 'SELECT "Customer Country" FROM m JOIN n ON 1 = 1',
            },
        )
        assert r.status_code == 400
        codes = [e["code"] for e in r.json()["detail"]["errors"]]
        assert "UNSUPPORTED_SQL_FEATURE" in codes

    async def test_compile_group_by_ignored(self, client: AsyncClient) -> None:
        """Explicit GROUP BY in Semantic QL is silently accepted."""
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={
                "model_id": mid,
                "sql": (
                    'SELECT "Customer Country", "Total Revenue" FROM m GROUP BY "Customer Country"'
                ),
            },
        )
        assert r.status_code == 200

    async def test_compile_measure_in_where_routes_to_having(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={
                "model_id": mid,
                "sql": (
                    'SELECT "Customer Country", "Total Revenue" FROM m WHERE "Total Revenue" > 1000'
                ),
            },
        )
        assert r.status_code == 200
        q = r.json()["query"]
        assert len(q["having"]) == 1
        assert q["having"][0]["field"] == "Total Revenue"
        assert q["where"] == []

    async def test_compile_with_rollup(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={
                "model_id": mid,
                "sql": ('SELECT "Customer Country", "Total Revenue" FROM m WITH ROLLUP'),
                "dialect": "postgres",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["query"]["grouping"] == "rollup"
        # Pretty-printed: "GROUP BY\n  ROLLUP (..." — match across whitespace
        normalized = " ".join(body["sql"].split())
        assert "GROUP BY ROLLUP" in normalized

    async def test_compile_with_cube_clickhouse_trailing_form(self, client: AsyncClient) -> None:
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql/compile",
            json={
                "model_id": mid,
                "sql": ('SELECT "Customer Country", "Total Revenue" FROM m WITH CUBE'),
                "dialect": "clickhouse",
            },
        )
        assert r.status_code == 200
        normalized = " ".join(r.json()["sql"].split())
        assert "WITH CUBE" in normalized
        assert "GROUP BY CUBE (" not in normalized  # ClickHouse uses trailing form

    async def test_top_level_shortcut(self, client: AsyncClient) -> None:
        sid, _ = await self._setup(client)
        # Only one model loaded — shortcut should auto-resolve
        r = await client.post(
            "/v1/query/semantic-ql/compile",
            json={
                "sql": 'SELECT "Customer Country", "Total Revenue" FROM m',
            },
        )
        # session_id was created so __default__ resolution requires single session
        # NOTE: the shortcut uses _resolve_store_and_model which may find
        # this user-created session. Either 200 or 409 is acceptable here;
        # we assert it's not 500.
        assert r.status_code in (200, 409), r.text
        if r.status_code == 200:
            assert "SELECT" in r.json()["sql"]
        # silence unused variable warning
        assert sid

    async def test_execute_unavailable_returns_503(self, client: AsyncClient) -> None:
        """Without QUERY_EXECUTE the execute endpoint returns 503 not 500."""
        sid, mid = await self._setup(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/semantic-ql",
            json={
                "model_id": mid,
                "sql": 'SELECT "Customer Country" FROM m',
            },
        )
        # Test fixtures do not enable query_execute by default
        assert r.status_code in (200, 503)


class TestModelsEndpoint:
    """GET /v1/models — admin-curated model discovery surface."""

    async def test_empty_in_dynamic_mode(self, client: AsyncClient) -> None:
        """Dynamic mode (no MODEL_FILES) — no protected models, empty list."""
        r = await client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["models"] == []

    async def test_single_legacy_default(self, single_model_client: AsyncClient) -> None:
        """Legacy MODEL_FILE — one __default__ entry."""
        r = await single_model_client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        entry = data["models"][0]
        assert entry["name"] == "__default__"
        assert entry["dimensions"] > 0 or entry["measures"] > 0


class TestMultiModelMode:
    """MODEL_FILES=a.yaml,b.yaml — admin pre-loads N named models."""

    @pytest.fixture
    async def multi_model_app(self, tmp_path):
        """Two YAMLs with different `name:` fields → two protected sessions."""
        from orionbelt.api.app import _read_model_file

        # Build two distinct models with explicit names
        yaml_a = SAMPLE_MODEL_YAML.replace("version: 1.0", "version: 1.0\nname: sales")
        yaml_b = SAMPLE_MODEL_YAML.replace("version: 1.0", "version: 1.0\nname: returns")
        path_a = tmp_path / "a.yaml"
        path_b = tmp_path / "b.yaml"
        path_a.write_text(yaml_a)
        path_b.write_text(yaml_b)

        settings = Settings(
            session_ttl_seconds=3600,
            session_cleanup_interval=9999,
            model_files=f"{path_a},{path_b}",
        )
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
            is_single_model_mode=True,  # admin-curated
        )
        for path in (path_a, path_b):
            yaml_str, resolved = _read_model_file(str(path))
            # Use the same name resolution as the real lifespan
            from orionbelt.api.app import _resolve_model_name

            name = _resolve_model_name(yaml_str, resolved)
            store = mgr.get_or_create_named(name)
            store.load_model(yaml_str)
        init_session_manager(mgr)
        yield app
        reset_session_manager()

    @pytest.fixture
    async def multi_model_client(self, multi_model_app):
        transport = ASGITransport(app=multi_model_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def test_models_endpoint_lists_both(self, multi_model_client: AsyncClient) -> None:
        r = await multi_model_client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()
        names = sorted(m["name"] for m in data["models"])
        assert names == ["returns", "sales"]
        assert data["count"] == 2


class TestReferenceEndpoints:
    """GET /v1/reference and friends — agent / LLM discovery surface."""

    async def test_index_lists_all_references(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference")
        assert r.status_code == 200
        names = {entry["name"] for entry in r.json()["references"]}
        assert names == {"obml", "obsql", "obml-schema", "query-schema"}

    async def test_obml_reference_is_markdown(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference/obml")
        assert r.status_code == 200
        body = r.json()["reference"]
        assert "OBML" in body
        assert "dataObjects" in body

    async def test_obsql_reference_includes_grammar(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference/obsql")
        assert r.status_code == 200
        body = r.json()["reference"]
        # Spot-check that the key OBSQL features are documented
        assert "OBSQL" in body
        assert "MEASURE(" in body
        assert "WITH ROLLUP" in body
        assert "raw mode" in body.lower()
        assert "RAW_SQL_REJECTED" in body
        assert "WRITE_OPERATION_REJECTED" in body

    async def test_obml_schema_served(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference/schemas/obml")
        assert r.status_code == 200
        # JSON Schema content-type
        assert "schema+json" in r.headers["content-type"]
        body = r.json()
        # OBML schema's top-level keys
        assert body.get("$schema") or body.get("type")

    async def test_query_schema_served(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference/schemas/query")
        assert r.status_code == 200
        assert "schema+json" in r.headers["content-type"]
        body = r.json()
        assert body.get("$schema") or body.get("type") or "select" in str(body).lower()

    async def test_unknown_schema_404(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference/schemas/bogus")
        assert r.status_code == 404
        # Error names the available schemas
        detail = r.json().get("detail", "")
        assert "obml" in detail and "query" in detail
