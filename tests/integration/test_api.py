"""Integration tests for the FastAPI REST API."""

from __future__ import annotations

from pathlib import Path

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

    async def test_dialects_supported_aggregations(self, client: AsyncClient) -> None:
        """The response states what a dialect *can* do.

        A client asking "may I use median here?" would otherwise have to fetch
        the OBML aggregation vocabulary separately and subtract.
        """
        from orionbelt.models.semantic import AggregationType

        response = await client.get("/v1/dialects")
        by_name = {d["name"]: d for d in response.json()["dialects"]}
        # MySQL and Dremio cannot compute mode.
        assert "mode" not in by_name["mysql"]["supported_aggregations"]
        assert "mode" not in by_name["dremio"]["supported_aggregations"]
        # ``measure`` (Databricks Metric View delegation) is supported on
        # Databricks alone (v2.7.7+, see #92).
        for name, info in by_name.items():
            if name == "databricks":
                assert "measure" in info["supported_aggregations"]
            else:
                assert "measure" not in info["supported_aggregations"], (
                    f"{name}: 'measure' must not be listed as supported"
                )
        # Postgres computes everything except ``measure``.
        assert by_name["postgres"]["supported_aggregations"] == sorted(
            a.value for a in AggregationType if a.value != "measure"
        )

    async def test_dialects_supported_functions(self, client: AsyncClient) -> None:
        """Each dialect lists the catalog minus what it declares unsupported.

        This used to assert every dialect rendered the whole catalog, on the
        grounds that an entry was admitted only once all eight could answer it,
        and noted that the field would earn its place when a later group left
        one behind. Nothing has yet: the json group briefly dropped Dremio and
        Databricks before both turned out to have a cast that declines rather
        than fails. Asserting the subtraction keeps this honest whenever one
        does.

        Asserting the subtraction rather than a hard-coded list keeps the test
        honest as further entries are dropped, while still failing if the route
        stops subtracting at all.
        """
        import orionbelt.dialect  # noqa: F401  -- triggers registration
        from orionbelt.dialect.registry import DialectRegistry
        from orionbelt.models.functions import FUNCTION_CATALOG

        response = await client.get("/v1/dialects")
        payload = response.json()["dialects"]
        assert payload, "no dialects returned"
        for info in payload:
            unsupported = {
                f.lower()
                for f in DialectRegistry.get(info["name"]).capabilities.unsupported_functions
            }
            expected = sorted(n for n in FUNCTION_CATALOG if n.lower() not in unsupported)
            assert info["supported_functions"] == expected, info["name"]

        by_name = {d["name"]: d for d in payload}
        for dialect in ("duckdb", "databricks", "dremio"):
            assert "json_value" in by_name[dialect]["supported_functions"], dialect

    async def test_published_examples_match_the_response_shape(self) -> None:
        """The docs and the GPT action spec show this endpoint's response.

        Renaming a field and leaving the examples behind is how a published
        contract becomes a lie: the response fields were replaced by their
        positive counterparts and three surfaces went on showing the old ones.
        """
        import yaml

        from orionbelt.api.schemas import DialectInfo

        repo_root = Path(__file__).resolve().parents[2]
        fields = set(DialectInfo.model_fields)
        retired = {"unsupported_aggregations", "unsupported_functions"}

        spec = yaml.safe_load(
            (repo_root / "integrations/chatgpt-custom-gpt/openapi-gpt-action.yaml").read_text()
        )
        documented = spec["paths"]["/v1/dialects"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["dialects"]["items"]["properties"]
        assert set(documented) == fields

        for doc in ("docs/api/endpoints.md", "docs/guide/dialects.md"):
            text = (repo_root / doc).read_text()
            assert not (retired & set(text.split())), f"{doc} still shows a retired field"
            for field in fields - {"name"}:
                assert field in text, f"{doc} omits '{field}' from the documented response"

    async def test_dialects_capabilities_are_boolean_only(self, client: AsyncClient) -> None:
        """The lists are their own fields; ``capabilities`` stays feature flags."""
        response = await client.get("/v1/dialects")
        for info in response.json()["dialects"]:
            assert all(isinstance(v, bool) for v in info["capabilities"].values())
            assert "unsupported_aggregations" not in info
            assert "unsupported_functions" not in info


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
        # v2.7.1 restored the v2.6 contract: when MODEL_FILES has exactly
        # one entry, ``model_yaml`` is populated for UI consumers. Multi-
        # model deployments (N > 1) still return None.
        assert data.get("model_yaml"), "single-MODEL_FILES must expose model_yaml for UI"
        assert "dataObjects" in data["model_yaml"]
        assert data["session_ttl_seconds"] == 3600  # from fixture
        # Admin-curated mode adds the model_settings + timezone blocks.
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
        model's choices.

        Uses admin-curated mode (``MODEL_FILES`` → one named protected
        session) and queries ``/v1/settings?session_id=<name>`` so the
        model-specific blocks pin to the loaded model.
        """
        from orionbelt.api.app import create_app

        yaml_text = (
            "version: 1.0\n"
            "name: orders\n"
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
        model_path = tmp_path / "model.yaml"
        model_path.write_text(yaml_text)

        settings = Settings(
            session_ttl_seconds=3600,
            session_cleanup_interval=9999,
            model_files=str(model_path),
        )
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
            is_single_model_mode=True,
        )
        store = mgr.get_or_create_named("orders")
        store.load_model(yaml_text)
        init_session_manager(mgr, admin_curated=True, db_vendor="duckdb")
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                response = await c.get("/v1/settings?session_id=orders")
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
                    "aggregation": "sum",
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
                        "aggregation": "sum",
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

    async def test_declared_boolean_is_reported_as_boolean(self, client: AsyncClient) -> None:
        """A declared boolean reported as "string" made the response metadata
        contradict its own data on every engine returning a real bool."""
        from orionbelt.api.query_cache import RESULT_TYPE_TO_HINT

        assert RESULT_TYPE_TO_HINT["boolean"] == "boolean"

    async def test_a_boolean_hint_is_not_numeric(self, client: AsyncClient) -> None:
        """``is_numeric_type_hint`` is lexical, so a new hint could match by
        accident and start formatting booleans with a decimal pattern."""
        from orionbelt.service.value_formatting import is_numeric_type_hint

        assert is_numeric_type_hint("boolean") is False

    async def test_validate_is_offline_by_default(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sample model names WAREHOUSE.PUBLIC tables that exist nowhere,
        so a probe that ran would fail it."""

        def explode(*args: object, **kwargs: object) -> object:
            raise AssertionError("validate must not touch the datasource by default")

        monkeypatch.setattr("orionbelt.service.db_executor.execute_sql", explode)
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/validate", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        assert response.json()["valid"] is True

    async def test_validate_online_reports_datasource_drift(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orionbelt.service.db_executor import ExecutionError

        def missing(*args: object, **kwargs: object) -> object:
            raise ExecutionError("relation does not exist")

        monkeypatch.setattr("orionbelt.service.db_executor.execute_sql", missing)
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{sid}/validate?online=true&dialect=duckdb",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert {e["code"] for e in body["errors"]} == {"DATASOURCE_TABLE_MISSING"}

    async def test_shortcut_validate_online(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orionbelt.service.db_executor import ExecutionUnavailableError

        def unavailable(*args: object, **kwargs: object) -> object:
            raise ExecutionUnavailableError("no credentials configured")

        monkeypatch.setattr("orionbelt.service.db_executor.execute_sql", unavailable)
        response = await client.post(
            "/v1/validate?online=true&dialect=duckdb", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert [e["code"] for e in body["errors"]] == ["DATASOURCE_UNAVAILABLE"]

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


SINGLE_MODEL_PROTECTED_NAME = "sales"


@pytest.fixture
def single_model_app(tmp_path):
    """App in admin-curated mode with one named protected model.

    Mirrors the real ``MODEL_FILES=path`` startup path: the YAML is loaded
    into a named protected session (``sales``), uploads/removals on it are
    blocked, and ``single_model_mode`` in ``/v1/settings`` reports True.
    """
    yaml_with_name = SAMPLE_MODEL_YAML.replace(
        "version: 1.0", f"version: 1.0\nname: {SINGLE_MODEL_PROTECTED_NAME}", 1
    )
    model_path = tmp_path / "model.yaml"
    model_path.write_text(yaml_with_name)
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        model_files=str(model_path),
    )
    app = create_app(settings=settings)
    # Manually init (ASGITransport doesn't trigger lifespan)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
        is_single_model_mode=True,  # admin-curated
    )
    store = mgr.get_or_create_named(SINGLE_MODEL_PROTECTED_NAME)
    store.load_model(yaml_with_name)
    init_session_manager(mgr, admin_curated=True, preload_model_yaml=yaml_with_name)
    yield app
    reset_session_manager()


@pytest.fixture
async def single_model_client(single_model_app):
    transport = ASGITransport(app=single_model_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestSingleModelMode:
    """Admin-curated mode (single ``MODEL_FILES`` entry) routes BI tools
    at the named protected session; uploads/removals on it are blocked,
    user-created sessions remain unrestricted in spirit but still see the
    admin-curated flag (REST POST/DELETE on models is rejected globally
    while the flag is on)."""

    async def test_protected_session_lists_the_model(
        self, single_model_client: AsyncClient
    ) -> None:
        r = await single_model_client.get(f"/v1/sessions/{SINGLE_MODEL_PROTECTED_NAME}/models")
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_model_upload_blocked(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        response = await single_model_client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 403
        assert "model upload is disabled" in response.json()["detail"]

    async def test_model_removal_blocked(self, single_model_client: AsyncClient) -> None:
        models = (
            await single_model_client.get(f"/v1/sessions/{SINGLE_MODEL_PROTECTED_NAME}/models")
        ).json()
        mid = models[0]["model_id"]
        response = await single_model_client.delete(
            f"/v1/sessions/{SINGLE_MODEL_PROTECTED_NAME}/models/{mid}"
        )
        assert response.status_code == 403
        assert "model removal is disabled" in response.json()["detail"]

    async def test_query_against_protected_session(self, single_model_client: AsyncClient) -> None:
        models = (
            await single_model_client.get(f"/v1/sessions/{SINGLE_MODEL_PROTECTED_NAME}/models")
        ).json()
        mid = models[0]["model_id"]
        response = await single_model_client.post(
            f"/v1/sessions/{SINGLE_MODEL_PROTECTED_NAME}/query/sql",
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

    async def test_validate_still_works(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        response = await single_model_client.post(
            f"/v1/sessions/{sid}/validate",
            json={"model_yaml": SAMPLE_MODEL_YAML},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_delete_user_session_still_works(self, single_model_client: AsyncClient) -> None:
        sid = (await single_model_client.post("/v1/sessions")).json()["session_id"]
        response = await single_model_client.delete(f"/v1/sessions/{sid}")
        assert response.status_code == 204

    async def test_shortcut_schema_sees_protected_session(
        self, single_model_client: AsyncClient
    ) -> None:
        """Regression: shortcut routes must scan protected sessions, not just
        __default__ + user sessions. With MODEL_FILES the only model lives in
        a named protected session — ``GET /v1/schema`` previously 404'd."""
        response = await single_model_client.get("/v1/schema")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("data_objects"), "schema must include at least one data object"

    async def test_settings_exposes_model_yaml_for_ui(
        self, single_model_client: AsyncClient
    ) -> None:
        """v2.7.1 hotfix: /v1/settings must surface the MODEL_FILES YAML so the
        Gradio UI can render the read-only editor without scraping
        /v1/models."""
        r = await single_model_client.get("/v1/settings")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["single_model_mode"] is True
        assert body.get("model_yaml"), "model_yaml must be populated in single-model mode"
        assert "dataObjects" in body["model_yaml"]

    async def test_new_session_inherits_protected_model(
        self, single_model_client: AsyncClient
    ) -> None:
        """v2.7.1 hotfix: POST /v1/sessions re-loads the protected model into
        each new user session so the session-scoped compile / execute flow
        works without uploading (which is blocked with 403)."""
        r = await single_model_client.post("/v1/sessions")
        assert r.status_code == 201, r.text
        assert r.json()["model_count"] == 1, r.json()
        sid = r.json()["session_id"]
        # And the model is queryable via the session-scoped route.
        listing = await single_model_client.get(f"/v1/sessions/{sid}/models")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

    async def test_shortcut_query_sees_protected_session(
        self, single_model_client: AsyncClient
    ) -> None:
        """Companion regression for the compile shortcut against a protected
        session — uses ``_resolve_store_and_model`` which had the same bug."""
        response = await single_model_client.post(
            "/v1/query/sql?dialect=postgres",
            json={
                "select": {
                    "dimensions": ["Customer Country"],
                    "measures": ["Total Revenue"],
                },
            },
        )
        assert response.status_code == 200, response.text
        assert "SELECT" in response.json()["sql"]

    async def test_shortcut_ignores_transient_user_sessions(
        self, single_model_client: AsyncClient
    ) -> None:
        """Regression: in admin-curated mode the shortcut must resolve to the
        curated protected model even when transient user sessions exist.

        Each ``POST /v1/sessions`` re-loads a *copy* of the protected model into
        the new session (with a fresh uuid model_id). The unique-model shortcut
        used to scan those copies too and 409 with "Multiple models loaded
        across sessions" — observed intermittently on the demo deployment. The
        shortcut now scopes to protected sessions in single-model mode.
        """
        # Create two user sessions — each inherits its own copy of the model.
        for _ in range(2):
            r = await single_model_client.post("/v1/sessions")
            assert r.status_code == 201, r.text
            assert r.json()["model_count"] == 1

        # The shortcuts must still resolve the single curated model, not 409.
        schema = await single_model_client.get("/v1/schema")
        assert schema.status_code == 200, schema.text
        assert schema.json().get("data_objects")

        compiled = await single_model_client.post(
            "/v1/query/sql?dialect=postgres",
            json={"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}},
        )
        assert compiled.status_code == 200, compiled.text


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

    async def test_single_named_model(self, single_model_client: AsyncClient) -> None:
        """Admin-curated mode with one entry — one named entry under
        the model's resolved name."""
        r = await single_model_client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        entry = data["models"][0]
        assert entry["name"] == SINGLE_MODEL_PROTECTED_NAME
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


class TestModelFileRemovalWarning:
    """v2.7.0 removed the legacy MODEL_FILE env var. pydantic-settings silently
    ignores unknown env vars, so a deployment that still sets it would boot
    with no preloaded model — startup must log a deprecation warning."""

    def test_warning_logged_when_model_file_set(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging as _logging

        from orionbelt.api.app import _warn_if_legacy_model_file_set

        monkeypatch.setenv("MODEL_FILE", "/tmp/should-be-ignored.yaml")
        caplog.set_level(_logging.WARNING, logger="orionbelt.api.app")
        _warn_if_legacy_model_file_set()
        assert any(
            "MODEL_FILE" in rec.message and "v2.7.0" in rec.message for rec in caplog.records
        ), [r.message for r in caplog.records]

    def test_no_warning_when_model_file_unset(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging as _logging

        from orionbelt.api.app import _warn_if_legacy_model_file_set

        monkeypatch.delenv("MODEL_FILE", raising=False)
        caplog.set_level(_logging.WARNING, logger="orionbelt.api.app")
        _warn_if_legacy_model_file_set()
        assert not any("MODEL_FILE" in rec.message for rec in caplog.records)


class TestModelSettingsSurface:
    """``/v1/settings`` mirrors the model's ``settings:`` block by hand.

    A hand-written mirror drifts: it was four fields behind by the time anyone
    checked, having missed ``defaultLocale`` long ago and then ``queryTimezone``,
    ``weekStart`` and ``expressionMode`` as each landed. Pydantic drops an
    unknown key silently, so a model could set one and the API would answer as
    though it had not.
    """

    def test_the_mirror_carries_every_model_setting(self) -> None:
        """The check that was missing, rather than the four fields that were."""
        from orionbelt.api.schemas import ModelSettingsInfo
        from orionbelt.models.semantic import ModelSettings

        mirrored = {f.alias or name for name, f in ModelSettingsInfo.model_fields.items()}
        declared = {f.alias or name for name, f in ModelSettings.model_fields.items()}
        assert declared - mirrored == set(), "settings missing from GET /v1/settings"
        assert mirrored - declared == set(), "GET /v1/settings invents settings"

    def test_the_mirror_carries_the_same_defaults(self) -> None:
        """Matching names are not enough: matching defaults are the answer.

        The response drops nulls, so a mirror field defaulting to None while
        the model's defaults to 'monday' makes the same setting present or
        absent depending on whether the YAML happened to write a ``settings:``
        block at all -- the model's own defaults fill it in once the block
        exists. Reporting the settings in force means reporting them always.
        """
        from pydantic import BaseModel

        from orionbelt.api.schemas import ModelSettingsInfo
        from orionbelt.models.semantic import ModelSettings

        def defaults(model: type[BaseModel]) -> dict[str, object]:
            return {
                field.alias or name: getattr(field.default, "value", field.default)
                for name, field in model.model_fields.items()
            }

        assert defaults(ModelSettingsInfo) == defaults(ModelSettings)

    async def test_the_new_settings_reach_the_response(self, client: AsyncClient) -> None:
        model_yaml = """\
version: 1.0
settings:
  queryTimezone: Europe/Zagreb
  weekStart: sunday
  expressionMode: portable
  defaultLocale: de-DE
dataObjects:
  Orders:
    code: o
    columns:
      Order ID: {code: id, abstractType: string}
dimensions:
  Order ID: {dataObject: Orders, column: Order ID, resultType: string}
measures:
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    resultType: int
    aggregation: count
"""
        session = (await client.post("/v1/sessions")).json()["session_id"]
        loaded = await client.post(
            f"/v1/sessions/{session}/models", json={"model_yaml": model_yaml}
        )
        assert loaded.status_code in (200, 201), loaded.text

        response = await client.get(f"/v1/settings?session_id={session}")
        assert response.status_code == 200
        settings = response.json()["model_settings"]
        assert settings["queryTimezone"] == "Europe/Zagreb"
        assert settings["weekStart"] == "sunday"
        assert settings["expressionMode"] == "portable"
        assert settings["defaultLocale"] == "de-DE"


class TestReferenceEndpoints:
    """GET /v1/reference and friends — agent / LLM discovery surface."""

    async def test_index_lists_all_references(self, client: AsyncClient) -> None:
        r = await client.get("/v1/reference")
        assert r.status_code == 200
        names = {entry["name"] for entry in r.json()["references"]}
        assert names == {"obml", "obsql", "functions", "obml-schema", "query-schema"}

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

    async def test_function_catalog_is_discoverable(self, client: AsyncClient) -> None:
        """An agent composing an expression has to be able to ask which
        functions are portable, rather than guess and fail at the warehouse.
        """
        r = await client.get("/v1/reference/functions")
        assert r.status_code == 200
        body = r.json()
        by_name = {f["name"]: f for f in body["functions"]}
        assert {"substring", "concat", "length", "position", "split_part"} <= set(by_name)
        assert "WRONG_FUNCTION_ARITY" in body["escape_hatch"]

        concat = by_name["concat"]
        assert concat["signature"] == "concat(a, b, ...)"
        assert concat["max_args"] is None  # variadic
        # The pinned semantics are the reason the endpoint exists: NULL
        # propagation is not what DuckDB or Postgres do on their own.
        assert "NULL propagates" in (concat["semantics"] or "")
        assert {"call": "concat('a', NULL, 'c')", "expect": None} in concat["examples"]

    async def test_function_catalog_matches_the_compiler(self, client: AsyncClient) -> None:
        """The endpoint serves the catalog the dialects render from, not a copy."""
        from orionbelt.models.functions import FUNCTION_CATALOG

        r = await client.get("/v1/reference/functions")
        served = {f["name"] for f in r.json()["functions"]}
        assert served == set(FUNCTION_CATALOG)

    async def test_obml_reference_documents_the_catalog(self, client: AsyncClient) -> None:
        """MCP and LLM clients read this text rather than the endpoint."""
        r = await client.get("/v1/reference/obml")
        body = r.json()["reference"]
        assert "/v1/reference/functions" in body
        assert "WRONG_FUNCTION_ARITY" in body
        assert "concat" in body

    async def test_obml_reference_lists_every_catalog_entry(self, client: AsyncClient) -> None:
        """The section is generated from the catalog rather than written out.

        A hand-maintained list went stale the moment the numeric group landed:
        the text still said "String group" and named eleven functions while the
        compiler already rendered thirty-two.
        """
        from orionbelt.models.functions import FUNCTION_CATALOG

        r = await client.get("/v1/reference/obml")
        body = r.json()["reference"]
        missing = [
            spec.signature for spec in FUNCTION_CATALOG.values() if spec.signature not in body
        ]
        assert not missing, f"OBML reference omits catalog entries: {missing}"

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


# ---------------------------------------------------------------------------
# Composables (Artefacts Composability Resolution)
# ---------------------------------------------------------------------------


class TestComposablesEndpoint:
    """POST/GET /v1/sessions/{sid}/models/{mid}/composables — ACR."""

    async def _load(self, client: AsyncClient) -> tuple[str, str]:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        return sid, load.json()["model_id"]

    async def test_composables_for_query(self, client: AsyncClient) -> None:
        sid, mid = await self._load(client)
        r = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/composables",
            json={"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}},
        )
        assert r.status_code == 200
        data = r.json()
        assert set(data["anchorObjects"]) == {"Customers", "Orders"}
        assert "Order Count" in data["measures"]
        assert data["cflMeasures"] == []

    async def test_composables_empty_query_offers_all(self, client: AsyncClient) -> None:
        sid, mid = await self._load(client)
        r = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/composables",
            json={"select": {"dimensions": []}},
        )
        assert r.status_code == 200
        data = r.json()
        assert "Total Revenue" in data["measures"]
        assert "Customer Country" in data["dimensions"]
        assert "Revenue per Order" in data["metrics"]

    async def test_composables_get_by_anchor_name(self, client: AsyncClient) -> None:
        sid, mid = await self._load(client)
        r = await client.get(
            f"/v1/sessions/{sid}/models/{mid}/composables", params={"anchor": "Total Revenue"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["anchorObjects"] == ["Orders"]
        assert "Customer Country" in data["dimensions"]

    async def test_composables_unknown_model_404(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r = await client.get(f"/v1/sessions/{sid}/models/nope/composables", params={"anchor": "x"})
        assert r.status_code == 404


class TestDeclaredTypeReconciliationWiring:
    """The wiring the four review findings were about.

    Asserted structurally because each failure is silent: the cache path
    returned the engine's types while every test passed, and oneshot dropped
    the reconciliation warnings while still returning a valid envelope.
    """

    def _source(self, relative: str) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src" / "orionbelt"
        return (root / relative).read_text()

    def test_the_cache_path_reconciles_before_it_reads_rows(self) -> None:
        """``rows`` frees the Arrow table, so reconciling after it is never."""
        source = self._source("api/query_cache.py")
        reconcile = source.index("reconcile_to_declared")
        rows = source.index("rows=exec_result.rows")
        assert reconcile < rows, "reconciliation must precede the cache write"

    def test_the_miss_path_carries_its_skips_to_the_response(self) -> None:
        source = self._source("api/services/query_execution.py")
        assert "declared_skips=cached.declared_skips" in source

    def test_oneshot_returns_the_envelope_warnings(self) -> None:
        """Reconciliation warnings are produced while the response is built, so
        the pre-execution list predates them."""
        source = self._source("api/routers/oneshot.py")
        assert "warnings=envelope.warnings" in source
        assert "warnings=hit_envelope.warnings" in source

    def test_the_documented_type_vocabulary_includes_boolean(self) -> None:
        assert "'boolean'" in self._source("api/schemas.py")

    def test_the_query_reaches_the_declared_type_map(self) -> None:
        """Without it a coalesce alias is never declared."""
        source = self._source("api/services/query_execution.py")
        assert "declared_arrow_types(model, query)" in source
