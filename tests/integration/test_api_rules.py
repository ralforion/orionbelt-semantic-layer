"""Business rule endpoints: list with statistics, detail, compile one, compile all, shortcuts."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

MODEL_YAML = """\
version: 1.0
name: ruled_sales
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    columns:
      ID: {code: ID, abstractType: string}
      Category: {code: CATEGORY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
      Returned: {code: RETURNED, abstractType: float}
dimensions:
  Category: {dataObject: Orders, column: Category}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
  Returned Amount:
    columns: [{dataObject: Orders, column: Returned}]
    aggregation: sum
metrics:
  Return Rate:
    expression: "{[Returned Amount]} / NULLIF({[Revenue]}, 0)"
rules:
  Electronics Sale:
    description: Rows in the Electronics category
    condition: {field: Category, op: "=", value: Electronics}
  High Return Rate:
    grain: [Category]
    owner: finance
    condition: {field: Return Rate, op: ">", value: 0.1}
  Healthy Category:
    type: validation
    severity: error
    grain: [Category]
    condition:
      all:
        - {field: Revenue, op: ">", value: 0}
        - {not: {rule: High Return Rate}}
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


class TestList:
    async def test_lists_rules_with_statistics(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/rules")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["dialect"] == "duckdb"
        by_name = {rule["name"]: rule for rule in data["rules"]}
        assert by_name["Electronics Sale"]["level"] == "row"
        assert by_name["Electronics Sale"]["findings"] == "matches"
        assert by_name["High Return Rate"]["level"] == "aggregate"
        assert by_name["High Return Rate"]["measures"] == ["Return Rate"]
        assert by_name["Healthy Category"]["findings"] == "violations"
        assert by_name["Healthy Category"]["depends_on"] == ["High Return Rate"]
        assert by_name["Healthy Category"]["severity"] == "error"
        assert all(rule["executable"] for rule in data["rules"])
        assert data["statistics"] == {
            "total": 3,
            "by_type": {"classification": 2, "validation": 1},
            "by_level": {"row": 1, "aggregate": 2},
            "by_severity": {"error": 1},
            "executable": 3,
            "not_executable": 0,
        }

    async def test_dialect_override(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/rules", params={"dialect": "postgres"})
        assert r.json()["dialect"] == "postgres"

    async def test_model_without_rules(self, client: AsyncClient) -> None:
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models",
            json={"model_yaml": MODEL_YAML.split("rules:")[0]},
        )
        r = await client.get(f"/v1/sessions/{sid}/models/{load.json()['model_id']}/rules")
        assert r.status_code == 200
        assert r.json()["rules"] == [] and r.json()["statistics"]["total"] == 0


class TestDetailAndCompile:
    async def test_detail_carries_condition_and_query(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.get(f"{base}/rules/Healthy Category")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["condition"] == {
            "all": [
                {"field": "Revenue", "op": ">", "value": 0},
                {"not": {"rule": "High Return Rate"}},
            ]
        }
        assert data["query"]["select"] == {
            "dimensions": ["Category"],
            "measures": ["Revenue", "Return Rate"],
        }
        assert data["query"]["having"][0]["negated"] is True
        assert data["external_concept_mappings"] == []

    async def test_unknown_rule_is_404(self, client: AsyncClient) -> None:
        base = await _load(client)
        assert (await client.get(f"{base}/rules/Nope")).status_code == 404
        assert (await client.post(f"{base}/rules/Nope/compile")).status_code == 404

    async def test_compile_one(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.post(
            f"{base}/rules/High Return Rate/compile", json={"dialect": "postgres"}
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["dialect"] == "postgres"
        assert "HAVING" in data["sql"] and "GROUP BY" in data["sql"]
        assert data["findings"] == "matches"

    async def test_compile_without_body_uses_the_model_default(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.post(f"{base}/rules/Electronics Sale/compile")
        assert r.status_code == 200, r.text
        assert r.json()["dialect"] == "duckdb"
        assert "WHERE" in r.json()["sql"]

    async def test_unsupported_dialect_is_400(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.post(f"{base}/rules/Electronics Sale/compile", json={"dialect": "cobol"})
        assert r.status_code == 400

    async def test_compile_all(self, client: AsyncClient) -> None:
        base = await _load(client)
        r = await client.post(f"{base}/rules/compile")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["compiled"] == 3 and data["failed"] == 0
        assert {row["name"] for row in data["results"]} == {
            "Electronics Sale",
            "High Return Rate",
            "Healthy Category",
        }
        assert all(row["status"] == "compiled" and row["sql"] for row in data["results"])


class TestShortcuts:
    async def test_all_four(self, client: AsyncClient) -> None:
        await _load(client)
        assert (await client.get("/v1/rules")).json()["statistics"]["total"] == 3
        assert (await client.get("/v1/rules/Electronics Sale")).json()["level"] == "row"
        assert "SELECT" in (await client.post("/v1/rules/Electronics Sale/compile")).json()["sql"]
        assert (await client.post("/v1/rules/compile")).json()["compiled"] == 3

    async def test_without_a_model_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/rules")).status_code == 404
