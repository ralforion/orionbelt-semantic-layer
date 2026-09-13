"""External concept mapping discovery endpoints (PR 3 of the context/ontology plan).

``/concept-mappings`` lists and filters the model's links to external
ontology concepts, ``/concept-mappings/namespaces`` says which namespaces
a model links into, and ``/concept-mappings/unmapped`` reports the
artefacts still without a mapping. The describe endpoints carry each
artefact's mappings too. All three have top-level shortcuts.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

CORP = "https://ontology.example.com/business/"

MODEL_YAML = f"""\
version: 1.0
name: mapped_sales
ontology:
  prefixes:
    corp: "{CORP}"
    fibo: "https://spec.edmcouncil.org/fibo/ontology/"
externalConceptMappings:
  - concept: corp:SalesModel
    relation: exact
dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    externalConceptMappings:
      - concept: corp:Order
        relation: exact
        justification: curated
    columns:
      ID:
        code: ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
  Customers:
    code: CUSTOMERS
    database: EDW
    schema: SALES
    columns:
      ID:
        code: ID
        abstractType: string
dimensions:
  Order ID:
    dataObject: Orders
    column: ID
    externalConceptMappings:
      - concept: corp:OrderIdentifier
        relation: exact
  Customer ID:
    dataObject: Customers
    column: ID
measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    externalConceptMappings:
      - concept: corp:NetRevenue
        relation: exact
        source: enterprise-finance-ontology
        confidence: 0.9
      - concept: fibo:FBC/Revenue
        relation: broader
      - concept: https://schema.org/MonetaryAmount
        relation: related
  Order Total:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: max
metrics:
  Revenue Doubled:
    expression: "{{[Revenue]}} * 2"
    externalConceptMappings:
      - concept: corp:NetRevenue
        relation: narrower
"""


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


async def _load(client: AsyncClient) -> str:
    sid = (await client.post("/v1/sessions")).json()["session_id"]
    load = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": MODEL_YAML})
    assert load.status_code == 201, load.text
    return f"/v1/sessions/{sid}/models/{load.json()['model_id']}"


def _pairs(data: dict) -> set[tuple[str, str, str]]:
    return {(m["object"]["type"], m["object"]["name"], m["relation"]) for m in data["mappings"]}


class TestListAndFilter:
    async def test_lists_every_mapping_with_its_object(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 7
        assert data["concept"] is None
        assert _pairs(data) == {
            ("model", "mapped_sales", "exact"),
            ("dataObject", "Orders", "exact"),
            ("dimension", "Order ID", "exact"),
            ("measure", "Revenue", "exact"),
            ("measure", "Revenue", "broader"),
            ("measure", "Revenue", "related"),
            ("metric", "Revenue Doubled", "narrower"),
        }
        revenue = next(m for m in data["mappings"] if m["concept"] == "corp:NetRevenue")
        assert revenue["expanded_iri"] == CORP + "NetRevenue"
        assert revenue["source"] == "enterprise-finance-ontology"
        assert revenue["confidence"] == pytest.approx(0.9)

    async def test_filter_by_full_iri(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings", params={"concept": CORP + "NetRevenue"})
        data = r.json()
        assert data["concept"] == CORP + "NetRevenue"
        assert _pairs(data) == {
            ("measure", "Revenue", "exact"),
            ("metric", "Revenue Doubled", "narrower"),
        }

    async def test_filter_by_compact_iri(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings", params={"concept": "corp:NetRevenue"})
        assert r.json()["concept"] == CORP + "NetRevenue"
        assert r.json()["total"] == 2

    async def test_filter_by_unknown_concept_is_empty_not_an_error(
        self, client: AsyncClient
    ) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings", params={"concept": "corp:Nothing"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    @pytest.mark.parametrize("namespace", ["corp", CORP])
    async def test_filter_by_namespace_prefix_or_iri(
        self, client: AsyncClient, namespace: str
    ) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings", params={"namespace": namespace})
        assert r.json()["total"] == 5

    async def test_filter_by_relation_and_types(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings", params={"relation": "exact"})
        assert r.json()["total"] == 4
        r = await client.get(
            f"{base}/concept-mappings", params={"relation": "exact", "types": "measure,metric"}
        )
        assert _pairs(r.json()) == {("measure", "Revenue", "exact")}

    @pytest.mark.parametrize(
        "params, fragment",
        [
            ({"concept": "acme:Thing"}, "Unknown prefix"),
            ({"concept": "_:b0"}, "blank node"),
            ({"relation": "same"}, "Unknown relation"),
            ({"types": "column"}, "Unknown object type"),
        ],
    )
    async def test_bad_filters_are_422(
        self, client: AsyncClient, params: dict, fragment: str
    ) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings", params=params)
        assert r.status_code == 422
        assert fragment in r.json()["detail"]

    async def test_unknown_model_is_404(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        r = await client.get(f"/v1/sessions/{sid}/models/nope/concept-mappings")
        assert r.status_code == 404


class TestNamespaces:
    async def test_namespaces_most_used_first(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings/namespaces")
        assert r.status_code == 200
        data = r.json()
        assert data["prefixes"] == {
            "corp": CORP,
            "fibo": "https://spec.edmcouncil.org/fibo/ontology/",
        }
        rows = [
            (n["prefix"], n["namespace"], n["mapping_count"], n["object_count"])
            for n in data["namespaces"]
        ]
        assert rows == [
            ("corp", CORP, 5, 5),
            (None, "https://schema.org/", 1, 1),
            ("fibo", "https://spec.edmcouncil.org/fibo/ontology/", 1, 1),
        ]


class TestUnmapped:
    async def test_unmapped_covers_the_mappable_scope(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings/unmapped")
        assert r.status_code == 200
        data = r.json()
        assert data["types"] == ["model", "dataObject", "dimension", "measure", "metric"]
        assert [(o["type"], o["name"]) for o in data["objects"]] == [
            ("dataObject", "Customers"),
            ("dimension", "Customer ID"),
            ("measure", "Order Total"),
        ]
        assert data["total"] == 3
        # Synthesized counts are never listed: authors cannot map them.
        assert not any("Count" in o["name"] for o in data["objects"])

    async def test_unmapped_narrowed_by_types(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/concept-mappings/unmapped", params={"types": "measure"})
        assert [(o["type"], o["name"]) for o in r.json()["objects"]] == [("measure", "Order Total")]
        assert r.json()["types"] == ["measure"]


class TestDescribeResponsesCarryMappings:
    async def test_schema_and_detail_endpoints(self, client: AsyncClient) -> None:
        base = await _load(client)
        schema = (await client.get(f"{base}/schema")).json()
        assert schema["ontology_prefixes"]["corp"] == CORP
        assert [m["concept"] for m in schema["external_concept_mappings"]] == ["corp:SalesModel"]
        orders = next(o for o in schema["data_objects"] if o["name"] == "Orders")
        assert orders["external_concept_mappings"][0]["justification"] == "curated"
        revenue = next(m for m in schema["measures"] if m["name"] == "Revenue")
        assert len(revenue["external_concept_mappings"]) == 3
        count = next(m for m in schema["measures"] if m["name"] == "Orders Count")
        assert count["external_concept_mappings"] == []

        dim = (await client.get(f"{base}/dimensions/Order ID")).json()
        assert dim["external_concept_mappings"][0]["expanded_iri"] == CORP + "OrderIdentifier"
        meas = (await client.get(f"{base}/measures/Revenue")).json()
        assert {m["relation"] for m in meas["external_concept_mappings"]} == {
            "exact",
            "broader",
            "related",
        }
        met = (await client.get(f"{base}/metrics/Revenue Doubled")).json()
        assert met["external_concept_mappings"][0]["relation"] == "narrower"


class TestShortcuts:
    async def test_shortcuts_resolve_the_single_model(self, client: AsyncClient) -> None:
        await _load(client)
        r = await client.get("/v1/concept-mappings", params={"concept": "corp:NetRevenue"})
        assert r.status_code == 200 and r.json()["total"] == 2
        r = await client.get("/v1/concept-mappings/namespaces")
        assert r.status_code == 200 and r.json()["namespaces"][0]["prefix"] == "corp"
        r = await client.get("/v1/concept-mappings/unmapped", params={"types": "dimension"})
        assert r.status_code == 200 and r.json()["total"] == 1

    async def test_shortcut_without_a_model_is_404(self, client: AsyncClient) -> None:
        r = await client.get("/v1/concept-mappings")
        assert r.status_code == 404
