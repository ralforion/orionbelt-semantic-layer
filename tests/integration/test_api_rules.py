"""Business rule endpoints: list with statistics, detail, compile one, compile all, shortcuts."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.db_executor import ExecutionResult
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


# ───────────────────────────── evaluation ─────────────────────────────

_ORDERS_SQL = """\
CREATE SCHEMA IF NOT EXISTS SALES;
CREATE TABLE SALES.ORDERS (ID VARCHAR, CATEGORY VARCHAR, AMOUNT DOUBLE, RETURNED DOUBLE);
INSERT INTO SALES.ORDERS VALUES
    ('O1', 'Electronics', 100.0, 30.0),
    ('O2', 'Electronics', 200.0, 20.0),
    ('O3', 'Toys',         50.0,  1.0),
    ('O4', 'Books',         0.0,  0.0);
"""
# Return rate: Electronics 50/300 = 0.167 (> 0.1), Toys 0.02, Books NULL.
# Healthy Category (Revenue > 0 AND NOT Return Rate > 0.1) is violated by
# Electronics (high returns) and Books (no revenue): two violations.


class TestEvaluate:
    """Evaluation through the same pipeline as query/execute, on an in-memory DuckDB."""

    @pytest.fixture
    def executing_client(self):
        duckdb = pytest.importorskip("duckdb", reason="duckdb required to evaluate rules")
        from orionbelt.service.db_executor import duckdb_execution_result

        conn = duckdb.connect(":memory:")
        conn.execute(_ORDERS_SQL)

        def execute_sql(
            sql: str, *, dialect: str, tz: Any = None, override_db_tz: bool = False
        ) -> ExecutionResult:
            return duckdb_execution_result(conn.execute(sql), time.monotonic(), tz=tz)

        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(mgr, query_execute_enabled=True, db_vendor="duckdb")
        try:
            with patch("orionbelt.api.query_cache.execute_sql", execute_sql):
                yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        finally:
            reset_session_manager()
            conn.close()

    async def test_matches_of_a_classification_rule(self, executing_client: AsyncClient) -> None:
        async with executing_client as client:
            base = await _load(client)
            r = await client.post(f"{base}/rules/High Return Rate/evaluate", json={"limit": 50})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["findings"] == "matches" and data["limit"] == 50
            assert [c["name"] for c in data["columns"]] == ["Category", "Return Rate"]
            assert [row[0] for row in data["rows"]] == ["Electronics"]
            assert data["row_count"] == 1 and "HAVING" in data["sql"]

    async def test_violations_of_a_validation_rule(self, executing_client: AsyncClient) -> None:
        async with executing_client as client:
            base = await _load(client)
            r = await client.post(f"{base}/rules/Healthy Category/evaluate")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["findings"] == "violations" and data["severity"] == "error"
            assert sorted(row[0] for row in data["rows"]) == ["Books", "Electronics"]

    async def test_row_level_rule_lists_matching_dimension_values(
        self, executing_client: AsyncClient
    ) -> None:
        async with executing_client as client:
            base = await _load(client)
            r = await client.post(f"{base}/rules/Electronics Sale/evaluate")
            assert r.status_code == 200, r.text
            assert r.json()["rows"] == [["Electronics"]]

    async def test_report_over_every_rule(self, executing_client: AsyncClient) -> None:
        async with executing_client as client:
            base = await _load(client)
            r = await client.post(f"{base}/rules/evaluate", json={"limit": 5, "include_sql": True})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["summary"] == {
                "total": 3,
                "executed": 3,
                "compiled": 0,
                "skipped": 0,
                "failed": 0,
                "with_findings": 3,
            }
            by_name = {row["name"]: row for row in data["results"]}
            assert by_name["Healthy Category"]["finding_count"] == 2
            assert by_name["Healthy Category"]["columns"] == ["Category", "Revenue", "Return Rate"]
            assert by_name["High Return Rate"]["sql"]
            assert all(row["status"] == "executed" for row in data["results"])
            assert data["filters"]["limit"] == 5 and "include_sql" not in data["filters"]
            assert data["generated_at"] and data["elapsed_ms"] >= 0

    async def test_report_filters_and_dry_run(self, executing_client: AsyncClient) -> None:
        async with executing_client as client:
            base = await _load(client)
            r = await client.post(
                f"{base}/rules/evaluate", json={"types": ["validation"], "dry_run": True}
            )
            data = r.json()
            assert [row["name"] for row in data["results"]] == ["Healthy Category"]
            assert data["results"][0]["status"] == "compiled"
            assert data["summary"]["compiled"] == 1 and data["summary"]["executed"] == 0
            r = await client.post(
                f"{base}/rules/evaluate", json={"max_rules": 2, "include_rows": False}
            )
            data = r.json()
            assert len(data["results"]) == 2 and all(row["rows"] == [] for row in data["results"])

    async def test_shortcuts(self, executing_client: AsyncClient) -> None:
        async with executing_client as client:
            await _load(client)
            assert (await client.post("/v1/rules/Electronics Sale/evaluate")).status_code == 200
            assert (await client.post("/v1/rules/evaluate")).json()["summary"]["executed"] == 3


class TestEvaluateWithoutExecution:
    async def test_evaluate_is_503_but_dry_run_report_works(self, client: AsyncClient) -> None:
        base = await _load(client)
        assert (await client.post(f"{base}/rules/Electronics Sale/evaluate")).status_code == 503
        assert (await client.post(f"{base}/rules/evaluate")).status_code == 503
        r = await client.post(f"{base}/rules/evaluate", json={"dry_run": True})
        assert r.status_code == 200 and r.json()["summary"]["compiled"] == 3
