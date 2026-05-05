"""Integration tests for model discovery endpoints and query explain."""

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


@pytest.fixture
async def session_with_model(client: AsyncClient) -> tuple[str, str]:
    """Create a session and load the sample model, return (session_id, model_id)."""
    resp = await client.post("/v1/sessions")
    session_id = resp.json()["session_id"]
    resp = await client.post(
        f"/v1/sessions/{session_id}/models",
        json={"model_yaml": SAMPLE_MODEL_YAML},
    )
    model_id = resp.json()["model_id"]
    return session_id, model_id


# ---------------------------------------------------------------------------
# Session-scoped model discovery
# ---------------------------------------------------------------------------


class TestSchema:
    async def test_get_schema(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_id"] == mid
        assert data["version"] == 1.0
        assert len(data["data_objects"]) == 2
        assert len(data["dimensions"]) == 1
        assert len(data["measures"]) == 3
        assert len(data["metrics"]) == 2

    async def test_schema_data_object_detail(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/schema")
        data = resp.json()
        orders = next(o for o in data["data_objects"] if o["name"] == "Orders")
        assert orders["code"] == "ORDERS"
        assert orders["database"] == "WAREHOUSE"
        assert len(orders["columns"]) == 3
        assert orders["join_targets"] == ["Customers"]


class TestDimensions:
    async def test_list_dimensions(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/dimensions")
        assert resp.status_code == 200
        dims = resp.json()
        assert len(dims) == 1
        assert dims[0]["name"] == "Customer Country"
        assert dims[0]["data_object"] == "Customers"

    async def test_get_dimension(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/dimensions/Customer Country")
        assert resp.status_code == 200
        dim = resp.json()
        assert dim["column"] == "Country"
        assert dim["result_type"] == "string"

    async def test_get_dimension_not_found(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/dimensions/Nonexistent")
        assert resp.status_code == 404


class TestMeasures:
    async def test_list_measures(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/measures")
        assert resp.status_code == 200
        measures = resp.json()
        assert len(measures) == 3
        names = {m["name"] for m in measures}
        assert "Total Revenue" in names
        assert "Order Count" in names

    async def test_get_measure(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/measures/Total Revenue")
        assert resp.status_code == 200
        m = resp.json()
        assert m["aggregation"] == "sum"
        assert m["result_type"] == "float"
        assert len(m["columns"]) == 1

    async def test_get_measure_not_found(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/measures/Nonexistent")
        assert resp.status_code == 404

    async def test_measure_description_format_data_type_round_trip(
        self, client: AsyncClient
    ) -> None:
        """description / format / dataType set in OBML must reach the API.

        Regression for two pre-existing bugs:
        1) ``parser/resolver.py`` Measure() call dropped ``description``.
        2) ``MeasureDetail`` was missing a ``data_type`` field, so even when
           the model held it the response never carried it.
        """
        annotated = """\
version: "1.0"
dataObjects:
  Orders:
    code: orders
    columns:
      Amount:
        code: amount
        abstractType: float
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    resultType: float
    aggregation: sum
    description: 'Total revenue'
    format: '#,##0.00'
    dataType: 'decimal(18, 2)'
"""
        resp = await client.post("/v1/sessions")
        sid = resp.json()["session_id"]
        resp = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": annotated},
        )
        mid = resp.json()["model_id"]

        # Listing endpoint
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/measures")
        assert resp.status_code == 200
        revenue_list = next(m for m in resp.json() if m["name"] == "Revenue")
        assert revenue_list["description"] == "Total revenue"
        assert revenue_list["format"] == "#,##0.00"
        assert revenue_list["dataType"] == "decimal(18, 2)"

        # Single-name endpoint (different code path)
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/measures/Revenue")
        assert resp.status_code == 200
        revenue_single = resp.json()
        assert revenue_single["description"] == "Total revenue"
        assert revenue_single["format"] == "#,##0.00"
        assert revenue_single["dataType"] == "decimal(18, 2)"


class TestMetrics:
    async def test_list_metrics(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/metrics")
        assert resp.status_code == 200
        metrics = resp.json()
        assert len(metrics) == 2
        rpo = next(m for m in metrics if m["name"] == "Revenue per Order")
        assert rpo["component_measures"] == ["Total Revenue", "Order Count"]

    async def test_get_metric(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/metrics/Revenue per Order")
        assert resp.status_code == 200
        met = resp.json()
        assert "Total Revenue" in met["component_measures"]
        assert "Order Count" in met["component_measures"]


class TestExplain:
    async def test_explain_dimension(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/explain/Customer Country")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "dimension"
        assert any(item["type"] == "data_object" for item in data["lineage"])

    async def test_explain_measure(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/explain/Total Revenue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "measure"

    async def test_explain_metric(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/explain/Revenue per Order")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "metric"
        assert any(item["type"] == "measure" for item in data["lineage"])

    async def test_explain_not_found(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/explain/Nonexistent")
        assert resp.status_code == 404


class TestSearch:
    async def test_find_by_name(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/find",
            json={"query": "Revenue"},
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) >= 2  # Total Revenue, Grand Total Revenue, Revenue per/Share
        types = {r["type"] for r in results}
        assert "measure" in types

    async def test_find_filter_types(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/find",
            json={"query": "Country", "types": ["dimension"]},
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert all(r["type"] == "dimension" for r in results)

    async def test_find_exact_synonym_buckets_present(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        """Phase 4: response splits exact / synonym / fuzzy."""
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/find",
            json={"query": "Revenue"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "exact_matches" in data
        assert "synonym_matches" in data
        assert "fuzzy_matches" in data
        # Revenue produces exact name hits — fuzzy is empty
        assert data["fuzzy_matches"] == []
        assert len(data["exact_matches"]) >= 2

    async def test_find_fuzzy_fallback_on_misspelling(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        """Phase 4: misspelled query with no exact hits returns fuzzy candidates."""
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/find",
            json={"query": "Custmr Cuntry"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exact_matches"] == []
        assert data["synonym_matches"] == []
        assert len(data["fuzzy_matches"]) > 0
        # Each fuzzy match has score and reason
        m = data["fuzzy_matches"][0]
        assert "score" in m
        assert "reason" in m
        assert 0.0 <= m["score"] <= 1.0

    async def test_find_no_match_no_fuzzy(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        """Phase 4: garbage query returns all-empty buckets."""
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/find",
            json={"query": "ZZQQXZ_random_garbage_99"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exact_matches"] == []
        assert data["synonym_matches"] == []
        assert data["fuzzy_matches"] == []


class TestJoinGraph:
    async def test_join_graph(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/join-graph")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["nodes"]) == {"Customers", "Orders"}
        assert len(data["edges"]) == 1
        edge = data["edges"][0]
        assert edge["from_object"] == "Orders"
        assert edge["to_object"] == "Customers"
        assert edge["cardinality"] == "many-to-one"


# ---------------------------------------------------------------------------
# Examples — PLAN_agent_api_improvements §5
# ---------------------------------------------------------------------------


_MODEL_WITH_EXAMPLES = (
    SAMPLE_MODEL_YAML
    + """
examples:
  - name: revenue_by_country
    description: Total completed-order revenue by customer country.
    intent_tags: [revenue, geography]
    query:
      select:
        dimensions: [Customer Country]
        measures: [Total Revenue]

  - name: order_count_by_country
    description: Number of orders per customer country.
    intent_tags: [orders, geography]
    query:
      select:
        dimensions: [Customer Country]
        measures: [Order Count]
"""
)


class TestExamples:
    async def _load_with_examples(self, client: AsyncClient) -> tuple[str, str]:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES}
        )
        assert load.status_code == 201, load.text
        return sid, load.json()["model_id"]

    async def test_list_examples_returns_summaries(self, client: AsyncClient) -> None:
        sid, mid = await self._load_with_examples(client)
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/examples")
        assert resp.status_code == 200
        data = resp.json()
        names = [e["name"] for e in data["examples"]]
        assert "revenue_by_country" in names
        assert "order_count_by_country" in names

    async def test_list_examples_intent_filter_match(self, client: AsyncClient) -> None:
        sid, mid = await self._load_with_examples(client)
        resp = await client.get(
            f"/v1/sessions/{sid}/models/{mid}/examples", params={"intent": "revenue"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["examples"]) == 1
        assert data["examples"][0]["name"] == "revenue_by_country"

    async def test_list_examples_intent_filter_miss_returns_suggestion(
        self, client: AsyncClient
    ) -> None:
        sid, mid = await self._load_with_examples(client)
        resp = await client.get(
            f"/v1/sessions/{sid}/models/{mid}/examples", params={"intent": "xyz_unknown"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["examples"] == []
        assert data["suggestion"] is not None
        assert "available tags" in data["suggestion"]

    async def test_get_single_example_returns_compiled_sql(self, client: AsyncClient) -> None:
        sid, mid = await self._load_with_examples(client)
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/examples/revenue_by_country")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "revenue_by_country"
        assert "select" in data["query"]
        # Compiled SQL preview is best-effort but should succeed for the sample model
        assert data["compiled_sql_preview"] is not None
        assert "SELECT" in data["compiled_sql_preview"]

    async def test_get_unknown_example_returns_404(self, client: AsyncClient) -> None:
        sid, mid = await self._load_with_examples(client)
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/examples/no_such_example")
        assert resp.status_code == 404

    async def test_model_without_examples_returns_empty_list(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/examples")
        assert resp.status_code == 200
        assert resp.json()["examples"] == []


# ---------------------------------------------------------------------------
# Query explain
# ---------------------------------------------------------------------------


class TestQueryExplain:
    async def test_compile_includes_explain(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": ["Total Revenue"],
                    }
                },
                "dialect": "postgres",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "explain" in data
        explain = data["explain"]
        assert explain["planner"] == "Star Schema"
        assert explain["base_object"] == "Orders"
        assert len(explain["joins"]) == 1
        assert explain["joins"][0]["to_object"] == "Customers"
        assert "reason" in explain["joins"][0]
        assert "planner_reason" in explain
        assert "base_object_reason" in explain

    async def test_explain_dim_only_query(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        sid, mid = session_with_model
        resp = await client.post(
            f"/v1/sessions/{sid}/query/sql",
            json={
                "model_id": mid,
                "query": {
                    "select": {
                        "dimensions": ["Customer Country"],
                        "measures": [],
                    }
                },
                "dialect": "postgres",
            },
        )
        assert resp.status_code == 200
        explain = resp.json()["explain"]
        assert explain["planner"] == "Star Schema"


# ---------------------------------------------------------------------------
# Top-level shortcut endpoints
# ---------------------------------------------------------------------------


class TestShortcuts:
    async def test_shortcut_schema(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data_objects"]) == 2

    async def test_shortcut_dimensions(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/dimensions")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_shortcut_dimension_detail(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/dimensions/Customer Country")
        assert resp.status_code == 200
        assert resp.json()["column"] == "Country"

    async def test_shortcut_measures(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/measures")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_shortcut_metrics(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/metrics")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_shortcut_explain(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/explain/Total Revenue")
        assert resp.status_code == 200
        assert resp.json()["type"] == "measure"

    async def test_shortcut_find(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.post("/v1/find", json={"query": "Revenue"})
        assert resp.status_code == 200
        assert len(resp.json()["results"]) >= 2

    async def test_shortcut_join_graph(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.get("/v1/join-graph")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 2

    async def test_shortcut_no_sessions(self, client: AsyncClient) -> None:
        resp = await client.get("/v1/schema")
        assert resp.status_code == 404

    async def test_shortcut_query_compile(
        self, client: AsyncClient, session_with_model: tuple[str, str]
    ) -> None:
        resp = await client.post(
            "/v1/query/sql?dialect=postgres",
            json={
                "select": {
                    "dimensions": ["Customer Country"],
                    "measures": ["Total Revenue"],
                }
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "SELECT" in data["sql"]
        assert data["explain"] is not None


# ---------------------------------------------------------------------------
# Owner field
# ---------------------------------------------------------------------------

SAMPLE_MODEL_WITH_OWNER = """\
version: 1.0
owner: team-data

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    owner: team-crm
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
        owner: team-geo

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string
    owner: team-analytics

measures:
  Customer Count:
    columns:
      - dataObject: Customers
        column: Customer ID
    resultType: int
    aggregation: count_distinct
    owner: team-analytics

metrics:
  Unique Customers:
    expression: '{[Customer Count]}'
    owner: team-analytics
"""


class TestOwnerField:
    async def test_owner_in_schema(self, client: AsyncClient) -> None:
        resp = await client.post("/v1/sessions")
        sid = resp.json()["session_id"]
        resp = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": SAMPLE_MODEL_WITH_OWNER},
        )
        assert resp.status_code == 201
        mid = resp.json()["model_id"]

        resp = await client.get(f"/v1/sessions/{sid}/models/{mid}/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert data["owner"] == "team-data"

        customers = next(o for o in data["data_objects"] if o["name"] == "Customers")
        assert customers["owner"] == "team-crm"

        country_col = next(c for c in customers["columns"] if c["name"] == "Country")
        assert country_col["owner"] == "team-geo"

        dim = data["dimensions"][0]
        assert dim["owner"] == "team-analytics"

        measure = data["measures"][0]
        assert measure["owner"] == "team-analytics"

        metric = data["metrics"][0]
        assert metric["owner"] == "team-analytics"
