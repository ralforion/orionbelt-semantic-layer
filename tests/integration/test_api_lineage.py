"""Lineage endpoints: per-type artefact routes, query lineage, formats, shortcuts."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from rdflib import Graph, URIRef

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

# "Revenue" is both a measure and a rule: names are unique per artefact type only.
MODEL_YAML = """\
version: 1.0
name: lineage_sales
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    columns:
      ID: {code: ID, abstractType: string, primaryKey: true}
      Customer: {code: CUSTOMER_ID, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
      Tax: {code: TAX, abstractType: float}
      Gross: {abstractType: float, expression: "{Amount} + {Tax}"}
      Order Date: {code: ORDER_DATE, abstractType: date}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer]
        columnsTo: [ID]
  Customers:
    code: CUSTOMERS
    database: EDW
    schema: SALES
    columns:
      ID: {code: ID, abstractType: string, primaryKey: true}
      Country: {code: COUNTRY, abstractType: string}
dimensions:
  Country: {dataObject: Customers, column: Country}
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
  US Gross:
    columns: [{dataObject: Orders, column: Gross}]
    aggregation: sum
    filters:
      - column: {dataObject: Customers, column: Country}
        operator: equals
        values: [{dataType: string, valueString: US}]
metrics:
  US Share:
    expression: "{[US Gross]} / {[Revenue]}"
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
rules:
  Revenue:
    grain: [Country]
    condition: {field: US Share, op: ">", value: 0.5}
"""


@pytest.fixture
def app():
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            max_age_seconds=settings.session_max_age_seconds,
            max_sessions=settings.max_sessions,
            max_models_per_session=settings.max_models_per_session,
            cleanup_interval=settings.session_cleanup_interval,
        )
    )
    yield app
    reset_session_manager()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _load(client: AsyncClient) -> tuple[str, str, str]:
    sid = (await client.post("/v1/sessions")).json()["session_id"]
    load = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": MODEL_YAML})
    assert load.status_code == 201, load.text
    model_id = load.json()["model_id"]
    return sid, model_id, f"/v1/sessions/{sid}/models/{model_id}"


def _edges(data: dict) -> set[tuple[str, str, str | None]]:
    return {(e["source"], e["target"], e["label"]) for e in data["edges"]}


class TestArtefactLineage:
    async def test_measure_reaches_its_table(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        r = await client.get(f"{base}/measures/Revenue/lineage")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["root"] == "measure:Revenue"
        assert ("column:Orders.Amount", "measure:Revenue", None) in _edges(data)
        assert ("data_object:Orders", "column:Orders.Amount", None) in _edges(data)
        tables = [n for n in data["nodes"] if n["kind"] == "data_object"]
        assert tables == [
            {
                "id": "data_object:Orders",
                "kind": "data_object",
                "name": "Orders",
                "detail": "EDW.SALES.ORDERS",
            }
        ]
        assert data["mermaid"].startswith("flowchart LR")

    async def test_rule_and_measure_of_the_same_name(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        rule = (await client.get(f"{base}/rules/Revenue/lineage")).json()
        measure = (await client.get(f"{base}/measures/Revenue/lineage")).json()
        assert rule["root"] == "rule:Revenue"
        assert measure["root"] == "measure:Revenue"
        assert ("metric:US Share", "rule:Revenue", "condition") in _edges(rule)
        assert ("dimension:Country", "rule:Revenue", "grain") in _edges(rule)

    async def test_metric_is_transitive(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        data = (await client.get(f"{base}/metrics/US Share/lineage")).json()
        edges = _edges(data)
        assert ("measure:US Gross", "metric:US Share", None) in edges
        assert ("column:Customers.Country", "measure:US Gross", "filter") in edges
        # A computed column reads its siblings
        assert ("column:Orders.Amount", "column:Orders.Gross", "expression") in edges
        assert ("column:Orders.Tax", "column:Orders.Gross", "expression") in edges

    async def test_cumulative_metric_names_its_time_dimension(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        data = (await client.get(f"{base}/metrics/Running Revenue/lineage")).json()
        assert ("dimension:Order Month", "metric:Running Revenue", "time") in _edges(data)

    async def test_dimension(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        data = (await client.get(f"{base}/dimensions/Country/lineage")).json()
        assert ("column:Customers.Country", "dimension:Country", None) in _edges(data)

    @pytest.mark.parametrize("kind", ["dimensions", "measures", "metrics", "rules"])
    async def test_unknown_name_is_404(self, client: AsyncClient, kind: str) -> None:
        _, _, base = await _load(client)
        r = await client.get(f"{base}/{kind}/Nope/lineage")
        assert r.status_code == 404
        assert "Nope" in r.json()["detail"]


class TestQueryLineage:
    QUERY = {
        "select": {"dimensions": ["Country"], "measures": ["US Share"]},
        "where": [{"field": "Country", "op": "!=", "value": "XX"}],
    }

    async def test_query_includes_planner_joins(self, client: AsyncClient) -> None:
        sid, model_id, _ = await _load(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/lineage",
            json={"model_id": model_id, "query": self.QUERY},
        )
        assert r.status_code == 200, r.text
        edges = _edges(r.json())
        assert r.json()["root"] == "query:Query"
        assert ("metric:US Share", "query:Query", None) in edges
        assert ("dimension:Country", "query:Query", "where") in edges
        assert ("data_object:Orders", "data_object:Customers", "join on Customer = ID") in edges

    async def test_unresolvable_query_is_422(self, client: AsyncClient) -> None:
        sid, model_id, _ = await _load(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/lineage",
            json={"model_id": model_id, "query": {"select": {"measures": ["Nope"]}}},
        )
        assert r.status_code == 422


class TestFormats:
    async def test_mermaid_text(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        r = await client.get(f"{base}/measures/Revenue/lineage", params={"format": "mermaid"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/vnd.mermaid")
        assert r.text.startswith("flowchart LR")

    async def test_turtle_uses_the_model_graph_iris(self, client: AsyncClient) -> None:
        sid, model_id, base = await _load(client)
        r = await client.get(f"{base}/metrics/US Share/lineage", params={"format": "turtle"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/turtle")
        lineage = Graph().parse(data=r.text, format="turtle")
        model = Graph().parse(data=(await client.get(f"{base}/graph")).text, format="turtle")
        prov = "http://www.w3.org/ns/prov#wasDerivedFrom"
        derived = list(lineage.subject_objects(URIRef(prov)))
        assert derived
        model_subjects = set(model.subjects())
        for target, source in derived:
            assert target in model_subjects and source in model_subjects

    async def test_query_turtle(self, client: AsyncClient) -> None:
        sid, model_id, _ = await _load(client)
        r = await client.post(
            f"/v1/sessions/{sid}/query/lineage",
            params={"format": "turtle"},
            json={"model_id": model_id, "query": TestQueryLineage.QUERY},
        )
        assert r.status_code == 200, r.text
        assert "prov:Entity" in r.text

    async def test_unknown_format_is_422(self, client: AsyncClient) -> None:
        _, _, base = await _load(client)
        r = await client.get(f"{base}/measures/Revenue/lineage", params={"format": "svg"})
        assert r.status_code == 422


class TestShortcuts:
    async def test_shortcuts_resolve_the_single_model(self, client: AsyncClient) -> None:
        await _load(client)
        for path, root in (
            ("/v1/dimensions/Country/lineage", "dimension:Country"),
            ("/v1/measures/Revenue/lineage", "measure:Revenue"),
            ("/v1/metrics/US Share/lineage", "metric:US Share"),
            ("/v1/rules/Revenue/lineage", "rule:Revenue"),
        ):
            r = await client.get(path)
            assert r.status_code == 200, (path, r.text)
            assert r.json()["root"] == root
        r = await client.post("/v1/query/lineage", json=TestQueryLineage.QUERY)
        assert r.status_code == 200, r.text
        assert r.json()["root"] == "query:Query"
        r = await client.get("/v1/rules/Revenue/lineage", params={"format": "mermaid"})
        assert r.text.startswith("flowchart LR")
