"""A federated query through pgwire, Flight SQL, OBSQL and the CLI, and its lineage.

Each surface runs the same query on the model split over the ``shop`` and
``crm`` data sources and on the same model read from ``all``, which holds both
tables; the rows must be the same. The data sources are the ones
``test_federated_executor`` configures.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service.lineage import LineageBuilder, to_turtle  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from tests.unit.test_federated_executor import (  # noqa: E402, F401 - fixtures among them
    REVENUE_BY_REGION,
    _default_limits,
    client,
    single_model,
    sources,
)
from tests.unit.test_federation_planner import model_yaml  # noqa: E402

OBSQL = 'SELECT "Region", "Revenue", "Top Order", "Orders Count" FROM shop_model ORDER BY "Region"'
# Orders alone: on the federated model it runs on ``shop`` and is not split.
ONE_SOURCE = 'SELECT "Order Date", "Revenue" FROM shop_model ORDER BY "Order Date"'


@pytest.fixture
def api(client: Any) -> Any:  # noqa: F811 - the imported fixture
    """The REST API over the three data sources."""
    return client


def federated_model() -> str:
    return model_yaml(federated=True)


def postgres_default() -> str:
    """The federated model with Postgres as its default dialect.

    Its sources are DuckDB, so a query reading one of them is compiled for
    DuckDB, not for the dialect the model defaults to.
    """
    return federated_model().replace("defaultDialect: duckdb", "defaultDialect: postgres")


# Run a query on one source of each: the second has another default dialect.
ONE_SOURCE_MODELS = pytest.mark.parametrize(
    "yaml", [federated_model(), postgres_default()], ids=["duckdb default", "postgres default"]
)


def manager_with(yaml: str) -> SessionManager:
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    mgr.get_or_create_named("shop_model").load_model(yaml)
    return mgr


def data_rows(reply: bytes) -> list[tuple[str | None, ...]]:
    """The text values of every DataRow in a pgwire reply."""
    rows: list[tuple[str | None, ...]] = []
    offset = 0
    while offset < len(reply):
        tag = reply[offset : offset + 1]
        (length,) = struct.unpack("!I", reply[offset + 1 : offset + 5])
        body = reply[offset + 5 : offset + 1 + length]
        offset += 1 + length
        assert tag != b"E", body
        if tag != b"D":
            continue
        (count,) = struct.unpack("!H", body[:2])
        at = 2
        values: list[str | None] = []
        for _ in range(count):
            (size,) = struct.unpack("!i", body[at : at + 4])
            at += 4
            if size < 0:
                values.append(None)
                continue
            values.append(body[at : at + size].decode())
            at += size
        rows.append(tuple(values))
    return rows


def file_cache(path: Any) -> FileCache:
    return FileCache(
        cache_dir=str(path),
        max_value_bytes=10 * 1024 * 1024,
        max_disk_bytes=50 * 1024 * 1024,
        max_ttl_seconds=3600,
        sweep_interval_seconds=3600,
    )


@pytest.mark.usefixtures("sources")
class TestPgwire:
    def pgwire(self, yaml: str, sql: str = OBSQL, **router: Any) -> list[tuple[str | None, ...]]:
        from orionbelt.pgwire.router import SemanticRouter

        mgr = manager_with(yaml)
        try:
            reply = asyncio.run(
                SemanticRouter(session_manager=mgr, default_dialect="duckdb", **router).handle(
                    sql, database="shop_model"
                )
            )
        finally:
            mgr.stop()
        return data_rows(reply)

    def test_without_a_cache(self) -> None:
        rows = self.pgwire(federated_model())
        assert rows == self.pgwire(single_model())
        assert len(rows) == 3

    @ONE_SOURCE_MODELS
    def test_a_query_on_one_source(self, yaml: str) -> None:
        rows = self.pgwire(yaml, ONE_SOURCE)
        assert rows == self.pgwire(single_model(), ONE_SOURCE)
        assert len(rows) == 7

    def test_through_the_cache(self, tmp_path: Any) -> None:
        from orionbelt.api.deps import CacheRuntimeConfig

        cache = file_cache(tmp_path / "cache")
        config = CacheRuntimeConfig(
            backend="file",
            min_ttl_seconds=1,
            max_ttl_seconds=3600,
            unknown_policy="default_ttl",
            unknown_default_ttl_seconds=300,
        )
        first = self.pgwire(federated_model(), cache=cache, cache_config=config)
        # Two legs and the combined result.
        assert asyncio.run(cache.stats()).entry_count == 3
        assert self.pgwire(federated_model(), cache=cache, cache_config=config) == first
        assert first == self.pgwire(single_model())


@pytest.mark.usefixtures("sources")
class TestFlight:
    @pytest.fixture(autouse=True)
    def _tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("ob_flight")
        # A RecordBatchStream does not give its table back; keep the table.
        monkeypatch.setattr("ob_flight.server_execution.flight.RecordBatchStream", lambda t: t)

    def flight(
        self, yaml: str, sql: str = OBSQL
    ) -> tuple[pa.Table, pa.Schema, dict[str, Any] | None, str]:
        import threading

        from ob_flight.server import OBFlightServer

        mgr = manager_with(yaml)
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mgr
        server._default_dialect = "duckdb"
        server._lock = threading.Lock()
        server._cache = None
        server._cache_config = None
        try:
            sql, dialect, _, schema, _, cache_meta = server._prepare_sql(sql)
            table = server._execute_sql(sql, dialect, cache_meta=cache_meta, result_schema=schema)
        finally:
            mgr.stop()
        return table, schema, cache_meta, dialect

    def test_matches_the_single_source(self) -> None:
        table, schema, cache_meta, _ = self.flight(federated_model())
        assert cache_meta is not None and "federated" in cache_meta
        assert table.num_rows == 3
        assert table.schema == schema
        assert table.to_pylist() == self.flight(single_model())[0].to_pylist()

    @ONE_SOURCE_MODELS
    def test_a_query_on_one_source(self, yaml: str) -> None:
        table, _, cache_meta, dialect = self.flight(yaml, ONE_SOURCE)
        assert cache_meta is not None and cache_meta["source"].name == "shop"
        # Keyed and run in the source's dialect, not the model's default.
        assert dialect == "duckdb"
        assert table.to_pylist() == self.flight(single_model(), ONE_SOURCE)[0].to_pylist()
        assert table.num_rows == 7


class TestRestObsql:
    async def obsql(self, api: Any, yaml: str) -> Any:
        session = (await api.post("/v1/sessions")).json()["session_id"]
        loaded = await api.post(f"/v1/sessions/{session}/models", json={"model_yaml": yaml})
        assert loaded.status_code == 201, loaded.text
        response = await api.post(
            f"/v1/sessions/{session}/query/semantic-ql",
            json={"model_id": loaded.json()["model_id"], "sql": OBSQL},
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def test_matches_the_single_source(self, api: Any) -> None:
        federated = await self.obsql(api, federated_model())
        assert [leg["data_source"] for leg in federated["federation"]["legs"]] == ["shop", "crm"]
        single = await self.obsql(api, single_model())
        assert federated["rows"] == single["rows"]
        assert len(federated["rows"]) == 3


class TestRestValueSearch:
    async def values(self, api: Any, yaml: str, **body: Any) -> Any:
        session = (await api.post("/v1/sessions")).json()["session_id"]
        loaded = await api.post(f"/v1/sessions/{session}/models", json={"model_yaml": yaml})
        model_id = loaded.json()["model_id"]
        return await api.post(f"/v1/sessions/{session}/models/{model_id}/values", json=body)

    @ONE_SOURCE_MODELS
    async def test_a_dimension_on_one_source(self, api: Any, yaml: str) -> None:
        federated = await self.values(api, yaml, dimension="Region")
        single = await self.values(api, single_model(), dimension="Region")
        assert federated.status_code == 200, federated.text
        assert federated.json()["values"] == single.json()["values"]
        assert federated.json()["values"]

    async def test_a_filter_on_another_source_is_refused_by_name(self, api: Any) -> None:
        # A query with no measures does not split across sources.
        response = await self.values(
            api,
            federated_model(),
            dimension="Order Date",
            where=[{"field": "Region", "op": "=", "value": "EU"}],
        )
        assert response.status_code == 422
        assert response.json()["detail"]["errors"][0]["code"] == "FEDERATION_NO_MEASURES"


ORDERS_BY_DATE = {
    "select": {"dimensions": ["Order Date"], "measures": ["Revenue"]},
    "orderBy": [{"field": "Order Date"}],
}


class TestOneshot:
    async def batch(self, api: Any, yaml: str, query: dict[str, Any], **body: Any) -> Any:
        response = await api.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": yaml,
                "queries": [{"id": "q", "query": query}],
                "execute": True,
                **body,
            },
        )
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert result["status"] == "ok", result
        return result

    async def test_runs_across_sources_and_lists_the_legs(self, api: Any) -> None:
        federated = await self.batch(api, federated_model(), REVENUE_BY_REGION)
        single = await self.batch(api, single_model(), REVENUE_BY_REGION)

        assert federated["executed"] is True
        assert federated["rows"] == single["rows"]
        assert len(federated["rows"]) == 3
        assert [leg["data_source"] for leg in federated["federation"]["legs"]] == ["shop", "crm"]
        assert single["federation"] is None

    async def test_a_repeat_is_served_from_the_cache(self, api: Any) -> None:
        first = await self.batch(api, federated_model(), REVENUE_BY_REGION)
        again = await self.batch(api, federated_model(), REVENUE_BY_REGION)

        assert first["cached"] is False
        assert again["cached"] is True
        assert again["rows"] == first["rows"]

    async def test_compiling_only_lists_the_legs(self, api: Any) -> None:
        result = await self.batch(api, federated_model(), REVENUE_BY_REGION, execute=False)

        assert result["executed"] is False
        assert [leg["data_source"] for leg in result["federation"]["legs"]] == ["shop", "crm"]

    @ONE_SOURCE_MODELS
    async def test_a_query_on_one_source(self, api: Any, yaml: str) -> None:
        result = await self.batch(api, yaml, ORDERS_BY_DATE)
        single = await self.batch(api, single_model(), ORDERS_BY_DATE)

        assert result["dialect"] == "duckdb"
        assert result["federation"] is None
        assert result["rows"] == single["rows"]
        assert len(result["rows"]) == 7


class TestPlanExplain:
    async def plan(self, api: Any, yaml: str, query: dict[str, Any]) -> Any:
        session = (await api.post("/v1/sessions")).json()["session_id"]
        loaded = await api.post(f"/v1/sessions/{session}/models", json={"model_yaml": yaml})
        assert loaded.status_code == 201, loaded.text
        response = await api.post(
            f"/v1/sessions/{session}/query/plan",
            json={
                "model_id": loaded.json()["model_id"],
                "query": query,
                "include_database_explain": True,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def test_each_leg_is_explained_on_its_own_source(self, api: Any) -> None:
        plan = await self.plan(api, federated_model(), REVENUE_BY_REGION)

        assert plan["database_explain"] is None
        legs = plan["database_explain_legs"]
        assert [(leg["leg"], leg["data_source"], leg["dialect"]) for leg in legs] == [
            ("leg_0", "shop", "duckdb"),
            ("leg_1", "crm", "duckdb"),
        ]
        assert all(leg["explain_output"] for leg in legs)
        assert "orders" in legs[0]["compiled_sql"]
        assert "customers" in legs[1]["compiled_sql"]
        assert not [w for w in plan["warnings"] if w["code"] == "DATABASE_EXPLAIN_FAILED"]

    @ONE_SOURCE_MODELS
    async def test_a_query_on_one_source(self, api: Any, yaml: str) -> None:
        plan = await self.plan(api, yaml, ORDERS_BY_DATE)

        assert plan["database_explain_legs"] == []
        assert plan["database_explain"]["dialect"] == "duckdb"
        assert plan["database_explain"]["explain_output"]
        assert not [w for w in plan["warnings"] if w["code"] == "DATABASE_EXPLAIN_FAILED"]

    async def test_a_leg_that_cannot_be_explained_is_named(
        self, api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        session = (await api.post("/v1/sessions")).json()["session_id"]
        loaded = await api.post(
            f"/v1/sessions/{session}/models", json={"model_yaml": federated_model()}
        )
        # Loaded, then the crm database moves out from under it.
        monkeypatch.setenv("DB_CRM_DUCKDB_DATABASE", str(tmp_path / "gone" / "crm.duckdb"))
        response = await api.post(
            f"/v1/sessions/{session}/query/plan",
            json={
                "model_id": loaded.json()["model_id"],
                "query": REVENUE_BY_REGION,
                "include_database_explain": True,
            },
        )

        plan = response.json()
        assert [leg["data_source"] for leg in plan["database_explain_legs"]] == ["shop"]
        failed = [w for w in plan["warnings"] if w["code"] == "DATABASE_EXPLAIN_FAILED"]
        assert len(failed) == 1
        assert "crm" in failed[0]["message"]


@pytest.mark.usefixtures("sources")
class TestCli:
    def test_obsql(self) -> None:
        from orionbelt.cli import _local

        _, federated = _local.execute_obsql(federated_model(), OBSQL, None, limit=None)
        _, single = _local.execute_obsql(single_model(), OBSQL, None, limit=None)
        assert federated.rows == single.rows
        assert len(federated.rows) == 3

    @ONE_SOURCE_MODELS
    def test_a_query_on_one_source(self, yaml: str) -> None:
        from orionbelt.cli import _local

        compiled, federated = _local.execute_obsql(yaml, ONE_SOURCE, None, limit=None)
        assert compiled.federation is None
        assert compiled.dialect == "duckdb"
        _, single = _local.execute_obsql(single_model(), ONE_SOURCE, None, limit=None)
        assert federated.rows == single.rows
        assert len(federated.rows) == 7

    def test_query(self) -> None:
        from orionbelt.cli import _local

        query = QueryObject.model_validate(REVENUE_BY_REGION)
        compiled, federated = _local.execute_query(federated_model(), query, None, limit=None)
        assert compiled.federation is not None
        _, single = _local.execute_query(single_model(), query, None, limit=None)
        assert federated.rows == single.rows


class TestLineage:
    def lineage(self, yaml: str) -> Any:
        from orionbelt.service.model_store import ModelStore

        store = ModelStore()
        model_id = store.load_model(yaml).model_id
        model = store.get_model(model_id)
        return LineageBuilder(model).query(QueryObject.model_validate(REVENUE_BY_REGION))

    def test_each_table_names_its_data_source(self) -> None:
        graph = self.lineage(federated_model())
        tables = {n.name: n.data_source for n in graph.nodes if n.kind == "data_object"}
        assert tables == {"Orders": "shop", "Customers": "crm"}
        assert "source: shop" in graph.to_mermaid()
        assert 'obsl:dataSource "crm"' in to_turtle(graph, "m")

    def test_the_default_data_source(self) -> None:
        graph = self.lineage(single_model())
        tables = {n.name: n.data_source for n in graph.nodes if n.kind == "data_object"}
        assert tables == {"Orders": "all", "Customers": "all"}

    def test_a_model_that_names_none(self) -> None:
        graph = self.lineage(model_yaml(federated=False))
        assert all(n.data_source is None for n in graph.nodes)
        assert "source:" not in graph.to_mermaid()
