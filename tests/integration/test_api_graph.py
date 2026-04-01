"""Integration tests for OBSL graph and SPARQL API endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def app():  # type: ignore[no-untyped-def]
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    application = create_app(settings=settings)
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    init_session_manager(mgr)
    yield application
    reset_session_manager()


@pytest.fixture
async def client(app):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def session_with_model(client: AsyncClient) -> tuple[str, str]:
    resp = await client.post("/v1/sessions")
    session_id = resp.json()["session_id"]
    resp = await client.post(
        f"/v1/sessions/{session_id}/models",
        json={"model_yaml": SAMPLE_MODEL_YAML},
    )
    model_id = resp.json()["model_id"]
    return session_id, model_id


# ---------------------------------------------------------------------------
# GET /graph — Turtle output
# ---------------------------------------------------------------------------


async def test_get_graph_turtle(client: AsyncClient, session_with_model: tuple[str, str]) -> None:
    session_id, model_id = session_with_model
    resp = await client.get(f"/v1/sessions/{session_id}/models/{model_id}/graph")
    assert resp.status_code == 200
    assert "text/turtle" in resp.headers["content-type"]
    assert "obsl:SemanticModel" in resp.text
    assert "rdfs:label" in resp.text


async def test_get_graph_not_found(
    client: AsyncClient, session_with_model: tuple[str, str]
) -> None:
    session_id, _ = session_with_model
    resp = await client.get(f"/v1/sessions/{session_id}/models/bad/graph")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /sparql — SPARQL queries
# ---------------------------------------------------------------------------


async def test_sparql_select(client: AsyncClient, session_with_model: tuple[str, str]) -> None:
    session_id, model_id = session_with_model
    resp = await client.post(
        f"/v1/sessions/{session_id}/models/{model_id}/sparql",
        json={
            "query": """
                PREFIX obsl: <https://ralforion.com/ns/obsl#>
                PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
                SELECT ?label WHERE {
                    ?m a obsl:Measure ; rdfs:label ?label .
                }
            """
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "select"
    labels = {r["label"] for r in data["results"]}
    assert "Total Revenue" in labels
    assert "Order Count" in labels


async def test_sparql_ask(client: AsyncClient, session_with_model: tuple[str, str]) -> None:
    session_id, model_id = session_with_model
    resp = await client.post(
        f"/v1/sessions/{session_id}/models/{model_id}/sparql",
        json={"query": "PREFIX obsl: <https://ralforion.com/ns/obsl#> ASK { ?x a obsl:Dimension }"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "ask"
    assert data["boolean"] is True


async def test_sparql_reject_update(
    client: AsyncClient, session_with_model: tuple[str, str]
) -> None:
    session_id, model_id = session_with_model
    resp = await client.post(
        f"/v1/sessions/{session_id}/models/{model_id}/sparql",
        json={"query": "INSERT DATA { <x> <y> <z> }"},
    )
    assert resp.status_code == 400


async def test_sparql_invalid_query(
    client: AsyncClient, session_with_model: tuple[str, str]
) -> None:
    session_id, model_id = session_with_model
    resp = await client.post(
        f"/v1/sessions/{session_id}/models/{model_id}/sparql",
        json={"query": "THIS IS NOT SPARQL"},
    )
    assert resp.status_code == 400


async def test_sparql_not_found(client: AsyncClient, session_with_model: tuple[str, str]) -> None:
    session_id, _ = session_with_model
    resp = await client.post(
        f"/v1/sessions/{session_id}/models/bad/sparql",
        json={"query": "SELECT * WHERE { ?s ?p ?o }"},
    )
    assert resp.status_code == 404
