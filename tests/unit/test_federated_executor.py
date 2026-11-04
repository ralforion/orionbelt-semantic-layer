"""Running a federated query: legs on their data sources, then the DuckDB combine.

The REST tests configure three DuckDB files as named data sources: ``shop``
(orders) and ``crm`` (customers), and ``all`` holding both. A query on the
model split over ``shop`` and ``crm`` must return what the same query returns
on ``all``, through the whole execute path: compile, admission, legs on their
own sources, per-leg cache, normalisation and combine. The data is the
planner test's, which holds an order without a customer and a customer
without a region.
"""

from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.api.deps import (  # noqa: E402
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.compiler.federation import (  # noqa: E402
    FederatedLeg,
    FederatedPlan,
    KeyInjection,
    KeyMap,
)
from orionbelt.service import federated_executor as federation  # noqa: E402
from orionbelt.service.db_executor import (  # noqa: E402
    ExecutionResult,
    ExecutionUnavailableError,
)
from orionbelt.service.federated_executor import (  # noqa: E402
    AdmissionGate,
    FederatedLegError,
    FederationLimits,
)
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402
from tests.unit.test_federation_planner import (  # noqa: E402
    CUSTOMERS,
    ORDERS,
    REASONS,
    RETURNS,
    TICKETS,
    model_yaml,
    subquery_model,
)

REVENUE_BY_REGION = {
    "select": {"dimensions": ["Region"], "measures": ["Revenue", "Top Order", "Orders Count"]},
    "orderBy": [{"field": "Region"}],
}


@pytest.fixture(autouse=True)
def _default_limits() -> Iterator[None]:
    federation.configure(FederationLimits())
    yield
    federation.configure(FederationLimits())


@pytest.fixture
def sources(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``shop``, ``crm`` and ``all``: three DuckDB files as named data sources."""
    paths = {name: str(tmp_path / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [ORDERS, CUSTOMERS, TICKETS, RETURNS, REASONS]),
        ("shop", [ORDERS, RETURNS]),
        ("crm", [CUSTOMERS, TICKETS, REASONS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    for variable in ("DUCKDB_DATABASE", "DUCKDB_DATABASE_PATH", "MOTHERDUCK_ACCESS_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OBSL_DATABASES", "shop,crm,all")
    for name, path in paths.items():
        monkeypatch.setenv(f"DB_{name.upper()}_TYPE", "duckdb")
        monkeypatch.setenv(f"DB_{name.upper()}_DUCKDB_DATABASE", path)
    return paths


@pytest.fixture
async def client(sources: dict[str, str], tmp_path: Any) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=Settings(session_ttl_seconds=3600, session_cleanup_interval=9999))
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    init_session_manager(
        mgr,
        query_execute_enabled=True,
        db_vendor="duckdb",
        cache=FileCache(
            cache_dir=str(tmp_path / "cache"),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=3600,
        ),
        cache_config=CacheRuntimeConfig(
            backend="file",
            min_ttl_seconds=1,
            max_ttl_seconds=3600,
            unknown_policy="default_ttl",
            unknown_default_ttl_seconds=300,
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        reset_session_manager()
        mgr.stop()


def single_model(**model: Any) -> str:
    return model_yaml(federated=False, **model).replace(
        "  defaultDialect: duckdb\n", "  defaultDialect: duckdb\n  defaultDataSource: all\n"
    )


MEDIAN = "  Median Order: {columns: [{dataObject: Orders, column: Amount}], aggregation: median}\n"


async def execute(client: AsyncClient, yaml: str, query: dict[str, Any]) -> Any:
    session = (await client.post("/v1/sessions")).json()["session_id"]
    loaded = await client.post(f"/v1/sessions/{session}/models", json={"model_yaml": yaml})
    assert loaded.status_code == 201, loaded.text
    return await client.post(
        f"/v1/sessions/{session}/query/execute",
        json={"model_id": loaded.json()["model_id"], "query": query},
    )


class TestRestExecution:
    @pytest.mark.parametrize(
        "query",
        [
            REVENUE_BY_REGION,
            {
                "select": {"dimensions": ["Region"], "measures": ["Revenue per Order"]},
                "orderBy": [{"field": "Region"}],
            },
            {
                "select": {"dimensions": ["Region", "Order Date:month"], "measures": ["Revenue"]},
                "where": [{"field": "Region", "op": "equals", "value": "EU"}],
                "orderBy": [{"field": "Order Date"}],
            },
            {
                "select": {"dimensions": ["Region", "Order Date:month"]},
                "orderBy": [{"field": "Order Date"}, {"field": "Region"}],
            },
        ],
    )
    async def test_a_federated_query_returns_what_one_source_returns(
        self, client: AsyncClient, query: dict[str, Any]
    ) -> None:
        single = await execute(client, single_model(), query)
        federated = await execute(client, model_yaml(federated=True), query)

        assert single.status_code == 200, single.text
        assert federated.status_code == 200, federated.text
        assert single.json()["federation"] is None
        assert federated.json()["rows"] == single.json()["rows"]
        assert federated.json()["columns"] == single.json()["columns"]

    async def test_a_median_is_computed_from_the_fact_legs_rows(self, client: AsyncClient) -> None:
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Median Order", "Revenue"]},
            "orderBy": [{"field": "Region"}],
        }

        single = await execute(client, single_model(extra=MEDIAN), query)
        federated = await execute(client, model_yaml(federated=True, extra=MEDIAN), query)

        assert federated.status_code == 200, federated.text
        assert federated.json()["rows"] == single.json()["rows"]
        assert federated.json()["columns"] == single.json()["columns"]

    async def test_the_response_shows_where_each_leg_ran(self, client: AsyncClient) -> None:
        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        federation_info = response.json()["federation"]
        assert [leg["data_source"] for leg in federation_info["legs"]] == ["shop", "crm"]
        assert response.json()["dialect"] == "duckdb"

    async def test_a_repeated_query_is_served_whole_from_the_cache(
        self, client: AsyncClient
    ) -> None:
        from orionbelt.api import query_cache

        calls: list[str] = []
        combines: list[str] = []
        real_execute, real_combine = query_cache.execute_sql, federation.combine

        def counting_execute(sql: str, **kwargs: Any) -> ExecutionResult:
            calls.append(kwargs["source"].name)
            return real_execute(sql, **kwargs)

        def counting_combine(plan: Any, *args: Any) -> ExecutionResult:
            combines.append(plan.combine_sql)
            return real_combine(plan, *args)

        with (
            patch.object(query_cache, "execute_sql", counting_execute),
            patch.object(federation, "combine", counting_combine),
        ):
            first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
            again = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert first.json()["cached"] is False and again.json()["cached"] is True
        assert again.json()["rows"] == first.json()["rows"]
        assert again.json()["federation"] == first.json()["federation"]
        assert calls == ["shop", "crm"] and len(combines) == 1

    async def test_a_query_changing_only_the_combine_step_reuses_the_legs(
        self, client: AsyncClient
    ) -> None:
        from orionbelt.api import query_cache

        calls: list[str] = []
        real = query_cache.execute_sql

        def counting(sql: str, **kwargs: Any) -> ExecutionResult:
            calls.append(kwargs["source"].name)
            return real(sql, **kwargs)

        filtered = {**REVENUE_BY_REGION, "having": [{"field": "Revenue", "op": ">", "value": 100}]}
        with patch.object(query_cache, "execute_sql", counting):
            first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
            # HAVING runs in the combine step, so the legs' SQL is unchanged
            # and both are served from the cache.
            narrowed = await execute(client, model_yaml(federated=True), filtered)

        assert narrowed.json()["cached"] is False
        assert calls == ["shop", "crm"]
        assert narrowed.json()["rows"] == [r for r in first.json()["rows"] if float(r[1]) > 100]

    @pytest.mark.parametrize(
        "settings",
        [
            "  defaultTimezone: Asia/Tokyo\n",
            # The zone the legs read timestamps in, not the one they were written in.
            "  defaultTimezone: UTC\n  queryTimezone: Asia/Tokyo\n",
        ],
        ids=["default", "query"],
    )
    async def test_a_result_is_labeled_with_the_zone_its_legs_read_timestamps_in(
        self, client: AsyncClient, settings: str
    ) -> None:
        yaml = model_yaml(federated=True, settings=settings)
        first = await execute(client, yaml, REVENUE_BY_REGION)
        again = await execute(client, yaml, REVENUE_BY_REGION)

        assert again.json()["cached"] is True
        assert first.json()["timezone"] == again.json()["timezone"] == "Asia/Tokyo"

    async def test_a_hit_on_one_source_is_labeled_as_its_run(self, client: AsyncClient) -> None:
        # With the zone of the source's sessions, not the model's fallback.
        yaml = single_model(settings="  defaultTimezone: Asia/Tokyo\n")
        first = await execute(client, yaml, REVENUE_BY_REGION)
        again = await execute(client, yaml, REVENUE_BY_REGION)

        assert again.json()["cached"] is True
        assert again.json()["timezone"] == first.json()["timezone"]

    async def test_a_declared_zone_labels_its_source(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_ALL_TIMEZONE", "America/Chicago")
        first = await execute(client, single_model(), REVENUE_BY_REGION)
        again = await execute(client, single_model(), REVENUE_BY_REGION)

        assert again.json()["cached"] is True
        assert first.json()["timezone"] == again.json()["timezone"] == "America/Chicago"

    @pytest.mark.parametrize("request_zone", [None, "America/Chicago"], ids=["own", "requested"])
    async def test_one_source_read_in_a_query_zone_is_labeled_with_it(
        self,
        client: AsyncClient,
        sources: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        request_zone: str | None,
    ) -> None:
        # Its SQL returns wall-clock times in Tokyo, whatever zone the session
        # runs in (declared here) or the request names: 10:00 UTC is 19:00 there.
        monkeypatch.setenv("DB_ALL_TIMEZONE", "Europe/Berlin")
        con = duckdb.connect(sources["all"])
        con.execute("CREATE TABLE events AS SELECT 1 AS id, TIMESTAMP '2024-01-05 10:00:00' AS at")
        con.close()
        yaml = """\
version: 1.0
settings:
  defaultDialect: duckdb
  defaultDataSource: all
  defaultTimezone: UTC
  queryTimezone: Asia/Tokyo
dataObjects:
  Events:
    code: events
    columns:
      Event ID: {code: id, abstractType: int}
      At: {code: at, abstractType: timestamp}
dimensions:
  At: {dataObject: Events, column: At, resultType: timestamp}
measures:
  Events: {columns: [{dataObject: Events, column: Event ID}], aggregation: count}
"""
        query = {"select": {"dimensions": ["At"], "measures": ["Events"]}}
        session = (await client.post("/v1/sessions")).json()["session_id"]
        loaded = await client.post(f"/v1/sessions/{session}/models", json={"model_yaml": yaml})
        url = f"/v1/sessions/{session}/query/execute"
        params = {"timezone": request_zone} if request_zone else {}
        body = {"model_id": loaded.json()["model_id"], "query": query}
        first = (await client.post(url, json=body, params=params)).json()
        again = (await client.post(url, json=body, params=params)).json()

        assert again["cached"] is True
        for result in (first, again):
            assert result["timezone"] == "Asia/Tokyo"
            assert result["rows"] == [["2024-01-05T19:00:00+09:00", 1]]

    async def test_a_declared_zone_that_names_none_is_refused(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_ALL_TIMEZONE", "Mars/Olympus")
        response = await execute(client, single_model(), REVENUE_BY_REGION)

        assert response.status_code == 503
        assert "DB_ALL_TIMEZONE is 'Mars/Olympus'" in response.text

    async def test_a_filter_on_another_source_reruns_only_its_leg(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without key injection, which also reruns the fact leg on the keys."""
        from orionbelt.api import query_cache

        monkeypatch.setenv("DB_KEY_INJECTION_MAX_KEYS", "0")

        calls: list[str] = []
        real = query_cache.execute_sql

        def counting(sql: str, **kwargs: Any) -> ExecutionResult:
            calls.append(kwargs["source"].name)
            return real(sql, **kwargs)

        filtered = {
            **REVENUE_BY_REGION,
            "where": [{"field": "Region", "op": "equals", "value": "EU"}],
        }
        with patch.object(query_cache, "execute_sql", counting):
            first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
            narrowed = await execute(client, model_yaml(federated=True), filtered)

        assert calls == ["shop", "crm", "crm"]
        assert narrowed.json()["rows"] == [r for r in first.json()["rows"] if r[0] == "EU"]

    async def test_the_same_query_over_other_sources_is_another_entry(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, sources: dict[str, str]
    ) -> None:
        first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
        # ``crm`` now reads the single file: same legs' SQL, same combine
        # query, another database behind one leg.
        monkeypatch.setenv("DB_CRM_DUCKDB_DATABASE", sources["all"])
        con = duckdb.connect(sources["all"])
        con.execute("UPDATE customers SET region = 'APAC' WHERE region = 'US'")
        con.close()

        moved = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert first.json()["cached"] is False and moved.json()["cached"] is False
        assert "APAC" in [row[0] for row in moved.json()["rows"]]

    async def test_a_failing_leg_is_named_by_its_source(
        self, client: AsyncClient, sources: dict[str, str]
    ) -> None:
        con = duckdb.connect(sources["crm"])
        con.execute("DROP TABLE customers")
        con.close()

        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert response.status_code == 502
        assert "leg leg_1 on crm (duckdb) failed" in response.json()["detail"]

    async def test_a_leg_over_the_row_limit_is_refused(self, client: AsyncClient) -> None:
        federation.configure(FederationLimits(max_leg_rows=2))

        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "FEDERATION_LEG_TOO_LARGE" in detail
        assert "leg leg_0 on shop" in detail
        # Why it is as large as it is: a row per customer, beside the regions.
        assert (
            "a row per key of the join Orders.Order Customer -> Customers.Customer ID "
            "(between 'shop' and 'crm')"
        ) in detail
        assert "join the data sources on a column with fewer values" in detail

    async def test_a_leg_over_its_memory_is_refused(self, client: AsyncClient) -> None:
        federation.configure(FederationLimits(max_leg_bytes=1))

        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "FEDERATION_LEG_TOO_LARGE" in detail
        assert "in memory, the most one leg may hold (FEDERATION_MAX_LEG_MB)" in detail
        assert "a row per key of the join Orders.Order Customer -> Customers.Customer ID" in detail
        # Spilling is off: the advice says where larger legs could go.
        assert "set FEDERATION_SPILL_DIR to a directory on disk" in detail

    async def test_a_leg_served_from_the_cache_is_held_to_its_memory_too(
        self, client: AsyncClient
    ) -> None:
        first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
        assert first.status_code == 200
        federation.configure(FederationLimits(max_leg_bytes=1))
        streamed: list[str] = []
        stream = federation.LegSink.__call__

        def counted(sink: Any, reader: Any) -> Any:
            streamed.append(sink._leg.name)
            return stream(sink, reader)

        # A new combine step over the same legs: they come from the cache.
        with patch.object(federation.LegSink, "__call__", counted):
            again = await execute(
                client, model_yaml(federated=True), {**REVENUE_BY_REGION, "limit": 2}
            )

        assert streamed == []
        assert again.status_code == 502
        assert "FEDERATION_MAX_LEG_MB" in again.json()["detail"]

    async def test_a_spilled_leg_is_not_held_to_its_memory(
        self, client: AsyncClient, tmp_path: Any
    ) -> None:
        federation.configure(spilling(tmp_path, spill_threshold_bytes=1, max_leg_bytes=1))

        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert response.status_code == 200, response.text

    async def test_a_spilled_leg_holds_what_normalizing_converts_within_its_memory(
        self, client: AsyncClient, sources: dict[str, str], tmp_path: Any
    ) -> None:
        """PostgreSQL hands a ``numeric`` over as text: the leg spills the text,
        and the decimals it becomes are held in memory beside the file."""
        con = duckdb.connect(sources["shop"])
        con.execute("ALTER TABLE orders ALTER amount TYPE VARCHAR")
        con.close()
        federation.configure(spilling(tmp_path, spill_threshold_bytes=1, max_leg_bytes=1))
        fields = {"select": {"fields": ["Orders.Amount", "Customers.Region"]}}

        response = await execute(client, model_yaml(federated=True), fields)

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "leg leg_0 on shop" in detail
        assert "FEDERATION_MAX_LEG_MB" in detail

    async def test_a_raw_leg_over_the_limit_is_explained_by_its_rows(
        self, client: AsyncClient
    ) -> None:
        # Seven orders of five customers: by region the shop's leg returns a row
        # per customer, five; as fields, a row per order, seven.
        federation.configure(FederationLimits(max_leg_rows=5))
        fields = {"select": {"fields": ["Orders.Amount", "Customers.Region"]}}

        grouped = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
        raw = await execute(client, model_yaml(federated=True), fields)

        assert grouped.status_code == 200
        assert raw.status_code == 502
        detail = raw.json()["detail"]
        assert "leg leg_0 on shop" in detail
        assert "A query of fields (select.fields) reads its data source's rows" in detail
        assert "join the data sources on a column with fewer values" not in detail

    async def test_a_leg_standing_for_more_rows_than_the_limit_is_refused(
        self, client: AsyncClient
    ) -> None:
        """Five customer rows that stand for the seven orders: under the limit
        of six as returned, over it as the combine step reads them."""
        federation.configure(FederationLimits(max_leg_rows=6))
        overall = (
            "  Customers Overall: {columns: [{dataObject: Orders, column: Order Customer}], "
            "aggregation: count_distinct, resultType: int, total: true}\n"
        )
        query = {"select": {"dimensions": ["Region"], "measures": ["Customers Overall"]}}

        response = await execute(client, model_yaml(federated=True, extra=overall), query)

        assert response.status_code == 502
        assert "FEDERATION_LEG_TOO_LARGE" in response.json()["detail"]
        assert "leg leg_0 on shop (duckdb) stands for 7 rows" in response.json()["detail"]

    async def test_a_leg_over_the_row_limit_stays_refused_when_repeated(
        self, client: AsyncClient
    ) -> None:
        # The first run must not leave the cut-short leg in the cache for the
        # second to combine as if it were whole.
        federation.configure(FederationLimits(max_leg_rows=2))

        first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
        again = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert (first.status_code, again.status_code) == (502, 502)
        assert "FEDERATION_LEG_TOO_LARGE" in again.json()["detail"]

    async def test_a_query_waiting_past_the_timeout_is_refused_as_busy(
        self, client: AsyncClient
    ) -> None:
        federation.configure(FederationLimits(max_concurrent=1, queue_timeout_seconds=0.05))
        held = federation.admission_gate().request()
        try:
            response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
        finally:
            federation.admission_gate().release()

        assert held.done()
        assert response.status_code == 503
        assert "FEDERATION_BUSY" in response.json()["detail"]
        assert federation.admission_gate().waiting == 0
        assert federation.admission_gate().active == 0


def compiled_federated(*queries: dict[str, Any]) -> tuple[Any, str, Any]:
    from orionbelt.models.query import QueryObject
    from orionbelt.service.model_store import ModelStore

    store = ModelStore()
    model_id = store.load_model(model_yaml(federated=True)).model_id
    compiled = [
        store.compile_query(model_id, QueryObject.model_validate(q), "duckdb")
        for q in queries or (REVENUE_BY_REGION,)
    ]
    return store, model_id, compiled[0] if len(compiled) == 1 else compiled


EU_REVENUE = {
    "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Orders Count"]},
    "where": [{"field": "Region", "op": "equals", "value": "EU"}],
    "orderBy": [{"field": "Order Date"}],
}


def joined_map(*keys: int) -> str:
    """The fact leg joining *keys* as a map, each to the one row of the
    customers' leg, which returns no other column: it reads only those keys."""
    rows = ", ".join(f"({key}, 1)" for key in keys)
    return (
        f'INNER JOIN (SELECT * FROM (VALUES {rows}) AS "m"("k", "n")) AS "__map0" '
        'ON "Orders"."customer_id" = "__map0"."k"'
    )


class TestKeyInjection:
    """A filter on the other source narrows the fact leg to the keys it keeps."""

    @pytest.fixture
    def ran(self) -> Iterator[list[tuple[str, str]]]:
        """Each leg's data source and SQL, in the order they ran."""
        from orionbelt.api import query_cache

        ran: list[tuple[str, str]] = []
        real = query_cache.execute_sql

        def recording(sql: str, **kwargs: Any) -> ExecutionResult:
            ran.append((kwargs["source"].name, sql))
            return real(sql, **kwargs)

        with patch.object(query_cache, "execute_sql", recording):
            yield ran

    async def _assert_same(self, client: AsyncClient, query: dict[str, Any]) -> list[Any]:
        federated = await execute(client, model_yaml(federated=True), query)
        single = await execute(client, single_model(), query)
        assert federated.status_code == 200, federated.text
        assert federated.json()["rows"] == single.json()["rows"]
        return list(federated.json()["rows"])

    async def test_the_fact_leg_reads_the_keys_after_the_dimension_leg(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        rows = await self._assert_same(client, EU_REVENUE)

        assert [source for source, _ in ran[:2]] == ["crm", "shop"]
        assert joined_map(10, 13) in ran[1][1]
        assert sum(Decimal(r[1]) for r in rows) == Decimal("123.90")

    async def test_an_exists_on_the_other_source_narrows_the_fact_leg_too(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        """The revenue of customers with a ticket, and of those without one."""
        tickets = {"dataObject": "Tickets"}
        for op, keys in (("exists", (10, 12)), ("nonexists", None)):
            ran.clear()
            query = {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [{"field": "Customers.Customer ID", "op": op, "subquery": tickets}],
                "orderBy": [{"field": "Order Date"}],
            }
            single_yaml = subquery_model(False).replace(
                "  defaultDialect: duckdb\n", "  defaultDialect: duckdb\n  defaultDataSource: all\n"
            )

            federated = await execute(client, subquery_model(True), query)
            single = await execute(client, single_yaml, query)

            assert federated.status_code == 200, federated.text
            assert federated.json()["rows"] == single.json()["rows"]
            assert federated.json()["rows"]
            if keys is not None:
                assert [source for source, _ in ran[:2]] == ["crm", "shop"]
                assert joined_map(*keys) in ran[1][1]

    async def test_a_query_without_measures_reads_the_keys_too(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        """The months EU customers ordered in."""
        query = {**EU_REVENUE, "select": {"dimensions": ["Order Date:month"]}}

        rows = await self._assert_same(client, query)

        assert [source for source, _ in ran[:2]] == ["crm", "shop"]
        assert joined_map(10, 13) in ran[1][1]
        assert len(rows) == 2

    async def test_a_leg_returning_rows_reads_the_keys_too(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        query = {
            **EU_REVENUE,
            "select": {"dimensions": ["Order Date"], "measures": ["Median Order"]},
        }

        federated = await execute(client, model_yaml(federated=True, extra=MEDIAN), query)
        single = await execute(client, single_model(extra=MEDIAN), query)

        assert federated.status_code == 200, federated.text
        assert federated.json()["rows"] == single.json()["rows"]
        assert [source for source, _ in ran[:2]] == ["crm", "shop"]
        assert '"Orders"."customer_id" IN (10, 13)' in ran[1][1]

    async def test_more_keys_than_the_source_takes_read_every_key(
        self, client: AsyncClient, ran: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_SHOP_KEY_INJECTION_MAX_KEYS", "1")

        await self._assert_same(client, EU_REVENUE)

        (shop,) = [sql for source, sql in ran[:2] if source == "shop"]
        assert " IN (" not in shop

    async def test_a_source_setting_overrides_the_shared_one(
        self, client: AsyncClient, ran: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_KEY_INJECTION_MAX_KEYS", "0")
        monkeypatch.setenv("DB_SHOP_KEY_INJECTION_MAX_KEYS", "2")

        await self._assert_same(client, EU_REVENUE)

        (shop,) = [sql for source, sql in ran[:2] if source == "shop"]
        assert joined_map(10, 13) in shop

    async def test_no_key_reads_no_fact_row(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        query = {
            "select": {"measures": ["Revenue", "Orders Count"]},
            "where": [{"field": "Region", "op": "equals", "value": "nowhere"}],
        }

        rows = await self._assert_same(client, query)

        assert ran[1][0] == "shop" and ran[1][1].endswith("LIMIT 0")
        assert rows[0][1] == 0

    async def test_a_filter_true_for_null_reads_every_key(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        """The orphan order's NULL region passes it, and the leg has no key for it."""
        query = {**EU_REVENUE, "where": [{"field": "Region", "op": "is_null"}]}

        await self._assert_same(client, query)

        (shop,) = [sql for source, sql in ran[:2] if source == "shop"]
        assert " IN (" not in shop

    @staticmethod
    def _plan(injected: list[dict[str, list[Any]]]) -> FederatedPlan:
        fact = FederatedLeg(
            name="leg_0", data_source="shop", dialect="duckdb", sql="", data_objects=()
        )
        return FederatedPlan(
            legs=(fact,),
            combine_sql="",
            injections=(
                KeyInjection("leg_1", "__fk0_0", "int"),
                KeyInjection("leg_2", "__fk1_0", "string"),
            ),
            inject=lambda keys, maps: injected.append(keys) or fact,
        )

    @pytest.mark.parametrize(
        ("max_keys", "expected"),
        [
            (5, {"__fk1_0": ["a", "b"], "__fk0_0": [1, 2, 3]}),
            (4, {"__fk1_0": ["a", "b"]}),
            (1, None),
        ],
        ids=["both", "the_fewest_first", "neither"],
    )
    def test_the_fewest_keys_are_read_first_within_the_limit(
        self, max_keys: int, expected: dict[str, list[Any]] | None
    ) -> None:
        injected: list[dict[str, list[Any]]] = []
        tables = {
            "leg_1": pa.table({"__fk0_0": [3, 1, None, 2, 1]}),
            "leg_2": pa.table({"__fk1_0": ["b", "a", "b"]}),
        }

        federation.injected_fact_leg(self._plan(injected), tables, max_keys)

        assert injected == ([expected] if expected else [])

    @pytest.mark.parametrize(
        "values",
        [
            pa.array([Decimal("1"), Decimal("2.5")]),
            pa.array([1.0, 2.0]),
            pa.array(["1", "2"]),
        ],
        ids=["a_fraction", "floats", "text"],
    )
    def test_an_integer_key_read_as_another_type_is_not_listed(self, values: Any) -> None:
        injected: list[dict[str, list[Any]]] = []
        tables = {"leg_1": pa.table({"__fk0_0": values}), "leg_2": pa.table({"__fk1_0": [1]})}

        federation.injected_fact_leg(self._plan(injected), tables, 10)

        assert injected == []

    def test_a_string_key_holding_a_nul_is_not_listed(self) -> None:
        """Postgres ends a statement's text at a NUL, inside a literal too."""
        injected: list[dict[str, list[Any]]] = []
        tables = {
            "leg_1": pa.table({"__fk0_0": [1]}),
            "leg_2": pa.table({"__fk1_0": ["a", "a\x00b"]}),
        }

        federation.injected_fact_leg(self._plan(injected), tables, 10)

        assert injected == [{"__fk0_0": [1]}]

    @pytest.mark.parametrize(
        ("dialect", "width"),
        [("clickhouse", 30), ("bigquery", 120)],
        ids=["clickhouse_max_query_size", "bigquery_1024k_characters"],
    )
    def test_a_list_longer_than_the_engine_parses_is_not_read(
        self, dialect: str, width: int
    ) -> None:
        """ClickHouse refuses a statement past ``max_query_size`` (256 KiB),
        BigQuery one past 1,024K characters."""
        fact = FederatedLeg(
            name="leg_0", data_source="shop", dialect=dialect, sql="", data_objects=()
        )
        plan = FederatedPlan(
            legs=(fact,),
            combine_sql="",
            injections=(KeyInjection("leg_1", "__fk0_0", "string"),),
            inject=lambda keys, maps: replace(fact, sql=f"IN ({', '.join(keys['__fk0_0'])})"),
        )
        short = {"leg_1": pa.table({"__fk0_0": ["a" * 20]})}
        long = {"leg_1": pa.table({"__fk0_0": [f"{n:0{width}d}" for n in range(10_000)]})}

        assert federation.injected_fact_leg(plan, short, 10_000)[0].sql.startswith("IN (")
        assert federation.injected_fact_leg(plan, long, 10_000) == (fact, {})

    @pytest.mark.parametrize(
        ("dialect", "limit"),
        [
            ("postgres", 10_000),
            ("mysql", 10_000),
            ("clickhouse", 10_000),
            ("bigquery", 10_000),
            ("duckdb", 1_000),
            ("snowflake", 1_000),
            ("databricks", 1_000),
            ("dremio", 1_000),
        ],
    )
    def test_each_engine_lists_as_many_keys_as_measured(
        self, dialect: str, limit: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5 million rows over 1 million keys (``design/PLAN_federation.md``
        §4.3.1): BigQuery took 21 s for every key and 2.2 s for 10,000;
        Snowflake, Databricks and Dremio 0.4-2.1 s for every key, so 10,000
        gained little or lost."""
        monkeypatch.delenv("DB_KEY_INJECTION_MAX_KEYS", raising=False)

        assert federation.key_injection_max_keys(None, dialect) == limit

    def test_an_integral_decimal_key_is_listed_as_an_integer(self) -> None:
        injected: list[dict[str, list[Any]]] = []
        tables = {
            "leg_1": pa.table({"__fk0_0": pa.array([Decimal("2"), Decimal("10.00")])}),
            "leg_2": pa.table({"__fk1_0": pa.array([], pa.string())}),
        }

        federation.injected_fact_leg(self._plan(injected), tables, 10)

        assert injected == [{"__fk1_0": [], "__fk0_0": [2, 10]}]


class TestKeyMaps:
    """A small dimension leg the fact leg joins as a map of its keys."""

    async def test_the_fact_leg_joins_the_map_after_the_dimension_leg(
        self, client: AsyncClient, ran: list[tuple[str, str]]
    ) -> None:
        """Revenue by month and region: the fact leg groups by each key's
        region's ordinal, EU 1 (customers 10 and 13), US 2, no region 3."""
        query = {
            "select": {"dimensions": ["Order Date:month", "Region"], "measures": ["Revenue"]},
            "orderBy": [{"field": "Order Date"}, {"field": "Region"}],
        }

        federated = await execute(client, model_yaml(federated=True), query)
        single = await execute(client, single_model(), query)

        assert federated.status_code == 200, federated.text
        assert federated.json()["rows"] == single.json()["rows"]
        assert [source for source, _ in ran[:2]] == ["crm", "shop"]
        assert (
            'LEFT JOIN (SELECT * FROM (VALUES (10, 1), (11, 2), (12, 3), (13, 1)) AS "m"("k", "n"))'
        ) in ran[1][1]

    @pytest.fixture
    def ran(self) -> Iterator[list[tuple[str, str]]]:
        from orionbelt.api import query_cache

        ran: list[tuple[str, str]] = []
        real = query_cache.execute_sql

        def recording(sql: str, **kwargs: Any) -> ExecutionResult:
            ran.append((kwargs["source"].name, sql))
            return real(sql, **kwargs)

        with patch.object(query_cache, "execute_sql", recording):
            yield ran

    @staticmethod
    def _plan(
        read: list[tuple[dict[str, Any], dict[str, Any]]],
        *injections: KeyInjection,
        dialect: str = "duckdb",
        sql: str = "",
    ) -> FederatedPlan:
        fact = FederatedLeg(
            name="leg_0", data_source="shop", dialect=dialect, sql=sql, data_objects=()
        )
        return FederatedPlan(
            legs=(fact,),
            combine_sql="",
            injections=injections,
            key_maps=(KeyMap("leg_1", "__fk0_0"),),
            inject=lambda keys, maps: read.append((keys, maps)) or fact,
        )

    def test_rows_equal_but_for_the_key_share_an_ordinal_numbered_by_their_values(
        self,
    ) -> None:
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        table = pa.table(
            {
                "Region": ["US", "EU", None, "EU", "US"],
                "__fk0_0": pa.array([11, 10, 12, 13, None], pa.int32()),
            }
        )

        _, rows = federation.injected_fact_leg(self._plan(read), {"leg_1": table}, 10)

        assert read == [({}, {"__fk0_0": [(10, 1), (11, 2), (12, 3), (13, 1)]})]
        assert rows["leg_1"].to_pylist() == [
            {"Region": "EU", "__fk0_0": 1},
            {"Region": "US", "__fk0_0": 2},
            {"Region": None, "__fk0_0": 3},
        ]
        shuffled = table.take([4, 3, 2, 1, 0])
        federation.injected_fact_leg(self._plan(read), {"leg_1": shuffled}, 10)
        assert read[1] == read[0]

    def test_a_key_of_two_rows_maps_to_each(self) -> None:
        """A dimension leg the declared many-to-one join repeats: the fact
        rows of the key count in both groups, as the combine step's join
        counts them."""
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        table = pa.table({"Region": ["US", "EU"], "__fk0_0": [10, 10]})

        federation.injected_fact_leg(self._plan(read), {"leg_1": table}, 10)

        assert read == [({}, {"__fk0_0": [(10, 1), (10, 2)]})]

    def test_a_leg_of_keys_alone_maps_each_to_its_one_row(self) -> None:
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        table = pa.table({"__fk0_0": [3, 1, 2]})

        _, rows = federation.injected_fact_leg(self._plan(read), {"leg_1": table}, 10)

        assert read == [({}, {"__fk0_0": [(1, 1), (2, 1), (3, 1)]})]
        assert rows["leg_1"].to_pylist() == [{"__fk0_0": 1}]

    @pytest.mark.parametrize(
        ("values", "max_keys"),
        [
            (pa.array([1, 2, 3]), 2),
            (pa.array([Decimal("1"), Decimal("2.5")]), 10),
            (pa.array([1.0, 2.0]), 10),
            (pa.array([], pa.int64()), 10),
        ],
        ids=["past_the_limit", "a_fraction", "floats", "no_key"],
    )
    def test_no_map_past_the_limit_or_of_keys_not_all_integers(
        self, values: Any, max_keys: int
    ) -> None:
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        plan = self._plan(read)

        fact, rows = federation.injected_fact_leg(
            plan, {"leg_1": pa.table({"__fk0_0": values})}, max_keys
        )

        assert (fact, rows, read) == (plan.legs[0], {}, [])

    def test_a_map_takes_the_place_of_its_keys_list(self) -> None:
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        plan = self._plan(read, KeyInjection("leg_1", "__fk0_0", "int"))

        federation.injected_fact_leg(plan, {"leg_1": pa.table({"__fk0_0": [1, 2]})}, 10)

        assert read == [({}, {"__fk0_0": [(1, 1), (2, 1)]})]

    @pytest.mark.parametrize(
        ("max_keys", "expected"),
        [
            (5, ({"__fk1_0": ["a", "b"]}, {"__fk0_0": [(1, 1), (2, 1), (3, 1)]})),
            (4, ({"__fk1_0": ["a", "b"]}, {})),
        ],
        ids=["both", "the_fewest_first"],
    )
    def test_maps_and_lists_share_the_limit_the_fewest_keys_first(
        self, max_keys: int, expected: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        plan = self._plan(read, KeyInjection("leg_2", "__fk1_0", "string"))
        tables = {
            "leg_1": pa.table({"__fk0_0": [3, 1, 2]}),
            "leg_2": pa.table({"__fk1_0": ["b", "a"]}),
        }

        federation.injected_fact_leg(plan, tables, max_keys)

        assert read == [expected]

    def test_a_map_longer_than_the_engine_parses_is_not_read(self) -> None:
        read: list[tuple[dict[str, Any], dict[str, Any]]] = []
        plan = self._plan(read, dialect="clickhouse", sql="x" * (256 * 1024 + 1))

        fact, rows = federation.injected_fact_leg(plan, {"leg_1": pa.table({"__fk0_0": [1]})}, 10)

        assert len(read) == 1
        assert (fact, rows) == (plan.legs[0], {})


class TestCachedLegs:
    async def test_a_cached_leg_over_the_limit_is_refused(
        self, sources: dict[str, str], tmp_path: Any
    ) -> None:
        from orionbelt.api.query_cache import execute_federated_with_cache

        # Compiled first: the limit is part of each leg's SQL, so these two
        # share their legs and differ only in the combine step.
        store, model_id, (compiled, reordered) = compiled_federated(
            REVENUE_BY_REGION,
            {**REVENUE_BY_REGION, "orderBy": [{"field": "Region", "direction": "desc"}]},
        )
        cache = FileCache(
            cache_dir=str(tmp_path / "legs"),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=3600,
        )
        config = CacheRuntimeConfig(backend="file", min_ttl_seconds=1, unknown_policy="default_ttl")

        async def run(query: Any) -> Any:
            return await execute_federated_with_cache(
                store=store,
                model=store.get_model(model_id),
                compile_result=query,
                model_id=model_id,
                cache=cache,
                cache_config=config,
            )

        await run(compiled)
        federation.configure(FederationLimits(max_leg_rows=2))
        assert reordered.federation.legs == compiled.federation.legs

        with pytest.raises(FederatedLegError, match="FEDERATION_LEG_TOO_LARGE"):
            await run(reordered)

    async def test_a_result_from_cached_legs_expires_with_them(
        self, sources: dict[str, str], tmp_path: Any
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from orionbelt.api.query_cache import execute_federated_with_cache

        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = {"now": start}

        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return clock["now"]

        store, model_id, (compiled, reordered) = compiled_federated(
            REVENUE_BY_REGION,
            {**REVENUE_BY_REGION, "orderBy": [{"field": "Region", "direction": "desc"}]},
        )
        cache = FileCache(
            cache_dir=str(tmp_path / "legs"),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=3600,
        )
        # Every entry lives 300 seconds.
        config = CacheRuntimeConfig(backend="file", min_ttl_seconds=1, unknown_policy="default_ttl")

        async def run(query: Any) -> Any:
            return await execute_federated_with_cache(
                store=store,
                model=store.get_model(model_id),
                compile_result=query,
                model_id=model_id,
                cache=cache,
                cache_config=config,
            )

        with patch("orionbelt.cache.file.datetime", Clock):
            await run(compiled)
            clock["now"] = start + timedelta(seconds=299)
            assert not (await run(reordered)).cached
            # The legs have expired, so the result built from them has too.
            clock["now"] = start + timedelta(seconds=301)
            assert not (await run(reordered)).cached

    @pytest.mark.parametrize(
        ("step", "target"),
        [
            ("reading the cached legs", "orionbelt.cache.file._read_bytes"),
            ("combining", "orionbelt.service.federated_executor.combine"),
            ("encoding the result", "orionbelt.api.query_cache.encode_table"),
        ],
    )
    async def test_a_result_whose_legs_expire_while_it_is_made_is_not_cached(
        self, sources: dict[str, str], tmp_path: Any, step: str, target: str
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from orionbelt.api.query_cache import execute_federated_with_cache

        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = {"now": start}

        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return clock["now"]

        module, _, name = target.rpartition(".")
        original = getattr(__import__(module, fromlist=[name]), name)

        def slow(*args: Any) -> Any:
            # Six seconds pass in this step.
            clock["now"] += timedelta(seconds=6)
            return original(*args)

        store, model_id, (compiled, reordered) = compiled_federated(
            REVENUE_BY_REGION,
            {**REVENUE_BY_REGION, "orderBy": [{"field": "Region", "direction": "desc"}]},
        )
        cache = FileCache(
            cache_dir=str(tmp_path / "legs"),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=3600,
        )
        # Every entry lives 300 seconds.
        config = CacheRuntimeConfig(backend="file", min_ttl_seconds=1, unknown_policy="default_ttl")

        async def run(query: Any) -> Any:
            return await execute_federated_with_cache(
                store=store,
                model=store.get_model(model_id),
                compile_result=query,
                model_id=model_id,
                cache=cache,
                cache_config=config,
            )

        with patch("orionbelt.cache.file.datetime", Clock):
            await run(compiled)
            clock["now"] = start + timedelta(seconds=295)
            with patch(target, slow):
                assert not (await run(reordered)).cached
            # The legs expired at 300 s, while the result was being made.
            assert clock["now"] > start + timedelta(seconds=300), step
            assert not (await run(reordered)).cached


class TestCancellation:
    async def test_a_cancelled_query_keeps_its_slot_until_its_legs_finish(
        self, sources: dict[str, str]
    ) -> None:
        import asyncio

        from orionbelt.api import query_cache
        from orionbelt.cache.noop import NoopCache

        store, model_id, compiled = compiled_federated()
        started, finish = threading.Event(), threading.Event()
        real = query_cache.execute_sql

        def slow(sql: str, **kwargs: Any) -> ExecutionResult:
            started.set()
            finish.wait(5)
            return real(sql, **kwargs)

        gate = federation.admission_gate()
        with patch.object(query_cache, "execute_sql", slow):
            running = asyncio.ensure_future(
                query_cache.execute_federated_with_cache(
                    store=store,
                    model=store.get_model(model_id),
                    compile_result=compiled,
                    model_id=model_id,
                    cache=NoopCache(),
                    cache_config=CacheRuntimeConfig(),
                )
            )
            await asyncio.to_thread(started.wait, 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running

            assert gate.active == 1
            finish.set()
            for _ in range(200):
                if gate.active == 0:
                    break
                await asyncio.sleep(0.01)

        assert gate.active == 0


class TestAdmissionGate:
    def test_waiters_are_admitted_in_order_of_arrival(self) -> None:
        gate = AdmissionGate(1)
        first, second, third = gate.request(), gate.request(), gate.request()

        assert first.done() and not second.done() and not third.done()
        gate.release()
        assert second.done() and not third.done()
        gate.release()
        assert third.done()
        gate.release()
        assert gate.active == 0

    def test_a_withdrawn_waiter_is_skipped(self) -> None:
        gate = AdmissionGate(1)
        gate.request()
        gone, next_one = gate.request(), gate.request()

        gate.withdraw(gone)
        gate.release()

        assert gone.cancelled() and next_one.done()
        assert gate.waiting == 0

    def test_withdrawing_after_being_admitted_frees_the_slot(self) -> None:
        gate = AdmissionGate(1)
        held = gate.request()
        late = gate.request()
        gate.release()  # ``late`` is admitted as its caller gives up

        gate.withdraw(late)

        assert held.done() and gate.active == 0

    def test_threads_never_exceed_the_limit(self) -> None:
        gate = AdmissionGate(2)
        running = peak = 0
        lock = threading.Lock()

        def work() -> None:
            nonlocal running, peak
            gate.request().result(timeout=5)
            with lock:
                running += 1
                peak = max(peak, running)
            with lock:
                running -= 1
            gate.release()

        threads = [threading.Thread(target=work) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert peak <= 2 and gate.active == 0


def leg(*columns: tuple[str, str]) -> FederatedLeg:
    return FederatedLeg(
        name="leg_0",
        data_source="crm",
        dialect="postgres",
        sql="",
        data_objects=("Orders",),
        columns=columns,
    )


class TestLegTooLarge:
    def test_a_leg_of_rows_a_measure_reads_is_explained_by_them(self) -> None:
        federation.configure(FederationLimits(max_leg_rows=1))
        rows = replace(leg(("Amount", "decimal"), ("__rows", "int")), rows="__rows")

        with pytest.raises(FederatedLegError) as refused:
            federation.check_leg_rows(rows, 2)

        assert "the rows of its data source a measure computed from rows reads" in str(
            refused.value
        )

    def test_a_leg_holding_no_join_names_none(self) -> None:
        federation.configure(FederationLimits(max_leg_rows=1))

        with pytest.raises(FederatedLegError) as refused:
            federation.check_leg_rows(leg(("Region", "string")), 2)

        assert "Filter the query, select fewer dimensions, or hold larger legs" in str(
            refused.value
        )
        assert "a row per key" not in str(refused.value)

    def test_a_leg_held_past_its_memory_is_refused_as_it_streams(self) -> None:
        federation.configure(FederationLimits(max_leg_bytes=1024 * 1024))
        table = batches(100_000)[1]  # 2.4 MB: an id and an amount, 24 bytes a row
        read: list[int] = []

        def pages() -> Iterator[Any]:
            for batch in table.to_batches(1000):
                read.append(batch.num_rows)
                yield batch

        reader = pa.RecordBatchReader.from_batches(table.schema, pages())

        with pytest.raises(FederatedLegError) as refused:
            federation.LegSink(leg(("Revenue", "decimal")))(reader)

        assert "holds more than 1 MB in memory" in str(refused.value)
        # Refused at the limit, not once the whole leg was read.
        assert sum(read) < 50_000

    def test_a_leg_spilling_to_disk_is_not_held_to_the_memory_limit(self, tmp_path: Any) -> None:
        limits = spilling(tmp_path, max_leg_bytes=1024 * 1024, spill_threshold_bytes=16 * 1024)
        federation.configure(limits)
        spill = federation.QuerySpill(limits)
        reader, table = batches(100_000)

        sink = federation.LegSink(leg(("Revenue", "decimal")), spill)
        out = sink(reader)

        assert sink.spilled
        assert out.equals(table)
        spill.close()

    def test_a_spilled_leg_holds_the_columns_normalizing_converts(self, tmp_path: Any) -> None:
        limits = spilling(tmp_path, max_leg_bytes=1024 * 1024, spill_threshold_bytes=1024)
        federation.configure(limits)
        spill = federation.QuerySpill(limits)
        amounts = pa.table({"Revenue": [f"{i}.25" for i in range(100_000)]})
        ids = batches(100_000)[1].select(["id"])

        def spilled(table: Any) -> Any:
            sink = federation.LegSink(leg(("Revenue", "decimal"), ("id", "int")), spill)
            out = sink(pa.RecordBatchReader.from_batches(table.schema, table.to_batches(1000)))
            assert sink.spilled
            return out

        # 0.8 MB of integers read from the file: nothing converted, nothing held.
        kept = federation.hold_leg(leg(("id", "int")), spilled(ids), spilled=True)
        assert kept.num_rows == 100_000
        # Text that becomes 1.6 MB of decimals in memory.
        with pytest.raises(FederatedLegError, match="holds more than 1 MB in memory"):
            federation.hold_leg(leg(("Revenue", "decimal")), spilled(amounts), spilled=True)
        spill.close()

    def test_a_spilled_leg_is_refused_before_converting_past_its_memory(
        self, tmp_path: Any
    ) -> None:
        limits = spilling(tmp_path, max_leg_bytes=1024 * 1024, spill_threshold_bytes=1024)
        federation.configure(limits)
        spill = federation.QuerySpill(limits)
        names = [f"m{i}" for i in range(20)]
        table = pa.table({n: [f"{i}.25" for i in range(20_000)] for n in names})
        numbers = leg(*((n, "decimal") for n in names))
        sink = federation.LegSink(numbers, spill)
        spilled = sink(pa.RecordBatchReader.from_batches(table.schema, table.to_batches(1000)))
        assert sink.spilled

        with (
            patch.object(
                federation, "_decimal_from_text", wraps=federation._decimal_from_text
            ) as conversions,
            pytest.raises(FederatedLegError, match="FEDERATION_MAX_LEG_MB"),
        ):
            federation.hold_leg(numbers, spilled, spilled=True)

        # 0.3 MB of decimals a column: three fit in 1 MB, and the fourth is
        # refused before it is made, not after all twenty.
        assert conversions.call_count == 3
        spill.close()

    def test_a_leg_in_memory_holds_its_whole_table(self) -> None:
        federation.configure(FederationLimits(max_leg_bytes=1024 * 1024))
        table = batches(100_000)[1]

        with pytest.raises(FederatedLegError, match="FEDERATION_MAX_LEG_MB"):
            federation.hold_leg(leg(("Revenue", "decimal")), table, spilled=False)

    def test_reading_buffers_again_as_another_type_allocates_nothing(self) -> None:
        sums = pa.chunked_array([pa.array([Decimal("1.25")] * 1000, pa.decimal128(38, 2))])
        variance = replace(leg(("s", "decimal")), integer_sums=(("s", 2),))

        out, converted = federation._normalize(variance, pa.table({"s": sums}))

        assert out.column("s").type == pa.decimal128(38, 0)
        assert converted == 0

    def test_with_spilling_on_the_advice_is_to_raise_the_limit(self, tmp_path: Any) -> None:
        federation.configure(spilling(tmp_path, max_leg_rows=1, max_leg_bytes=1))

        with pytest.raises(FederatedLegError) as rows:
            federation.check_leg_rows(leg(("Region", "string")), 2)
        with pytest.raises(FederatedLegError) as nbytes:
            federation.check_leg_bytes(leg(("Region", "string")), 2)

        assert str(rows.value).endswith(
            "or raise FEDERATION_MAX_LEG_ROWS: a leg past FEDERATION_SPILL_THRESHOLD_MB "
            "goes to disk."
        )
        assert str(nbytes.value).endswith("or raise FEDERATION_MAX_LEG_MB.")


MOUNTS = (
    "22 1 0:21 / / rw,relatime shared:1 - overlay overlay rw,lowerdir=/l,upperdir=/u\n"
    "30 22 0:30 / /scratch rw,nosuid shared:9 - tmpfs tmpfs rw,size=65536k\n"
    "31 22 8:1 / /data\\040disk rw shared:10 - ext4 /dev/sda1 rw\n"
    "32 30 8:2 / /scratch/disk rw shared:11 master:2 - xfs /dev/sdb1 rw\n"
)


class TestSpillDirectory:
    """Where legs spill by default: the temp directory, unless it is in memory."""

    @pytest.fixture(autouse=True)
    def _not_cloud_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for variable in ("K_SERVICE", "CLOUD_RUN_JOB"):
            monkeypatch.delenv(variable, raising=False)

    def test_a_directory_given_is_the_one_used(self) -> None:
        assert federation.spill_directory("/srv/spill") == "/srv/spill"

    @pytest.mark.parametrize("off", ["off", "OFF", " off "])
    def test_off_turns_spilling_off(self, off: str) -> None:
        assert federation.spill_directory(off) is None

    @pytest.mark.parametrize("unset", [None, "", "  "])
    def test_unset_it_is_the_temp_directory_on_disk(self, unset: str | None) -> None:
        import tempfile

        with patch.object(federation, "_filesystem_type", return_value="ext4"):
            assert federation.spill_directory(unset) == tempfile.gettempdir()

    def test_a_temp_directory_in_memory_spills_nothing(self) -> None:
        with patch.object(federation, "_filesystem_type", return_value="tmpfs"):
            assert federation.spill_directory(None) is None

    @pytest.mark.parametrize("variable", ["K_SERVICE", "CLOUD_RUN_JOB"])
    def test_on_cloud_run_nothing_spills_by_default(
        self, variable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(variable, "obsl")

        assert federation.spill_directory(None) is None
        # A volume given explicitly is the operator's choice.
        assert federation.spill_directory("/mnt/disk") == "/mnt/disk"

    def test_the_environment_defaults_to_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FEDERATION_SPILL_DIR", raising=False)
        monkeypatch.setenv("FEDERATION_MAX_LEG_MB", "64")

        with patch.object(federation, "spill_directory", return_value="/disk") as resolved:
            limits = FederationLimits.from_environment()

        resolved.assert_called_once_with(None)
        assert limits.spill_dir == "/disk"
        assert limits.max_leg_bytes == 64 * 1024 * 1024

    @pytest.mark.parametrize(
        ("path", "kind"),
        [
            ("/app/spill", "overlay"),
            ("/scratch", "tmpfs"),
            ("/scratch/obsl", "tmpfs"),
            # A mount point holds what is under it, not what merely starts alike.
            ("/scratchpad", "overlay"),
            ("/scratch/disk/obsl", "xfs"),
            ("/data disk/spill", "ext4"),
        ],
    )
    def test_the_filesystem_is_the_innermost_mount_holding_the_path(
        self, path: str, kind: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = tmp_path / "mountinfo"
        table.write_text(MOUNTS)
        monkeypatch.setattr(federation, "_MOUNTINFO", str(table))

        assert federation._filesystem_type(path) == kind

    def test_without_a_mount_table_the_filesystem_is_unknown(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(federation, "_MOUNTINFO", str(tmp_path / "none"))

        assert federation._filesystem_type("/tmp") is None


class TestNormalizeLeg:
    def test_a_zoned_timestamp_without_a_zone_is_its_wall_clock_time(self) -> None:
        # Databricks hands over 11:00 in Berlin, read in its UTC session, as
        # 11:00 UTC; DuckDB would read it in the server's zone.
        instant = pa.array([datetime(2024, 1, 5, 11, tzinfo=UTC), None], pa.timestamp("us", "UTC"))
        table = pa.table({"At": instant, "Logged": instant})

        out = federation.normalize_leg(leg(("At", "timestamp"), ("Logged", "timestamp_tz")), table)

        assert out.column("At").type == pa.timestamp("us")
        assert out.column("At").to_pylist() == [datetime(2024, 1, 5, 11), None]
        assert out.column("Logged").type == pa.timestamp("us", "UTC")

    def test_numbers_sent_as_text_become_exact_decimals(self) -> None:
        table = pa.table({"Revenue": ["100.50", "20.255", None, "-3"]})

        out = federation.normalize_leg(leg(("Revenue", "decimal")), table)

        assert out.schema.field("Revenue").type == pa.decimal128(38, 3)
        assert out.column("Revenue").to_pylist() == [
            Decimal("100.500"),
            Decimal("20.255"),
            None,
            Decimal("-3.000"),
        ]

    def test_postgres_numeric_is_read_through_its_opaque_type(self) -> None:
        opaque = pa.opaque(pa.string(), "numeric", "PostgreSQL")
        storage = pa.array(["1.25", "2.5"])
        table = pa.table({"Revenue": pa.ExtensionArray.from_storage(opaque, storage)})

        out = federation.normalize_leg(leg(("Revenue", "decimal")), table)

        assert out.column("Revenue").to_pylist() == [Decimal("1.25"), Decimal("2.50")]

    def test_a_value_below_one_may_use_all_38_digits_for_its_scale(self) -> None:
        table = pa.table({"Rate": ["0.00000000000000000000000000000000000001", "-0.5"]})

        out = federation.normalize_leg(leg(("Rate", "decimal")), table)

        assert out.schema.field("Rate").type == pa.decimal128(38, 38)
        assert out.column("Rate").to_pylist() == [Decimal("1E-38"), Decimal("-0.5")]

    def test_zero_needs_no_integer_digit(self) -> None:
        table = pa.table({"Rate": ["0.00000000000000000000000000000000000001", "0"]})

        out = federation.normalize_leg(leg(("Rate", "decimal")), table)

        assert out.schema.field("Rate").type == pa.decimal128(38, 38)
        assert out.column("Rate").to_pylist() == [Decimal("1E-38"), Decimal("0")]

    def test_a_value_no_decimal_holds_is_refused(self) -> None:
        table = pa.table({"Revenue": ["1", "NaN"]})

        with pytest.raises(FederatedLegError, match="FEDERATION_LEG_TYPE"):
            federation.normalize_leg(leg(("Revenue", "decimal")), table)

    def test_a_wide_decimal_narrows_when_its_values_fit(self) -> None:
        table = pa.table({"Revenue": pa.array([Decimal("1.25")], pa.decimal256(76, 2))})

        out = federation.normalize_leg(leg(("Revenue", "decimal")), table)

        assert out.schema.field("Revenue").type == pa.decimal128(38, 2)

    def test_a_wide_decimal_too_large_to_narrow_is_refused(self) -> None:
        table = pa.table({"Revenue": pa.array([Decimal("1" * 40)], pa.decimal256(76, 0))})

        with pytest.raises(FederatedLegError, match="more than 38 digits"):
            federation.normalize_leg(leg(("Revenue", "decimal")), table)

    def test_an_untyped_null_column_takes_its_declared_type(self) -> None:
        table = pa.table({"Region": pa.array([None], pa.null())})

        out = federation.normalize_leg(leg(("Region", "string")), table)

        assert out.schema.field("Region").type == pa.utf8()

    def test_a_date_sent_as_text_is_read_back_as_a_date(self) -> None:
        table = pa.table({"Order Date": ["2024-01-05", None]})

        out = federation.normalize_leg(leg(("Order Date", "date")), table)

        assert out.schema.field("Order Date").type == pa.date32()

    def test_rows_from_a_driver_without_arrow_combine_like_arrow(self) -> None:
        from orionbelt.service.db_executor import ColumnMeta

        result = ExecutionResult(
            columns=[ColumnMeta("Order Date", "datetime"), ColumnMeta("Revenue", "number")],
            raw_rows=[["2024-01-05", Decimal("100.50")], ["2024-01-20", Decimal("20.25")]],
            row_count=2,
            execution_time_ms=0,
        )

        out = federation.normalize_leg(
            leg(("Order Date", "date"), ("Revenue", "decimal")), federation.result_table(result)
        )

        assert out.schema.field("Order Date").type == pa.date32()
        assert out.column("Revenue").to_pylist() == [Decimal("100.50"), Decimal("20.25")]

    def test_text_of_a_text_column_is_left_alone(self) -> None:
        table = pa.table({"Region": ["EU"]})

        out = federation.normalize_leg(leg(("Region", "string")), table)

        assert out.equals(table)


class TestCombine:
    def test_the_combine_step_runs_within_its_limits(self) -> None:
        federation.configure(FederationLimits(combine_threads=1, combine_memory_limit="64MB"))
        plan = FederatedPlan(
            legs=(),
            combine_sql=(
                "SELECT current_setting('threads') AS threads, "
                "current_setting('memory_limit') AS memory"
            ),
        )

        result = federation.combine(plan, {}, None)

        threads, memory = result.rows[0]
        assert int(threads) == 1
        assert memory.replace(" ", "").upper().startswith(("61", "64"))

    def test_a_leg_may_stand_for_as_many_rows_as_it_may_return(self) -> None:
        federation.configure(FederationLimits(max_leg_rows=5))
        leg = FederatedLeg(
            name="leg_0",
            data_source="shop",
            dialect="duckdb",
            sql="",
            data_objects=("Orders",),
            rows="__rows",
        )
        plan = FederatedPlan(legs=(leg,), combine_sql="SELECT SUM(__rows) AS n FROM leg_0")

        five = federation.combine(plan, {"leg_0": pa.table({"__rows": [3, 2]})}, None)
        assert list(five.rows[0]) == [5]
        with pytest.raises(FederatedLegError, match="stands for 6 rows of its data source"):
            federation.combine(plan, {"leg_0": pa.table({"__rows": [3, 3]})}, None)

    def test_the_combine_step_spills_into_the_querys_directory(self, tmp_path: Any) -> None:
        federation.configure(FederationLimits(spill_max_bytes=100 * 1024 * 1024))
        plan = FederatedPlan(
            legs=(),
            combine_sql=(
                "SELECT current_setting('temp_directory') AS directory, "
                "current_setting('max_temp_directory_size') AS most"
            ),
        )

        result = federation.combine(plan, {}, None, str(tmp_path))

        directory, most = result.rows[0]
        assert directory == str(tmp_path)
        # Bounded by the query's spill maximum, in the MiB the limits count.
        assert most.replace(" ", "") in ("100MiB", "100.0MiB")


def batches(rows: int, per_batch: int = 1000) -> Any:
    """A record batch reader over *rows* rows of an id and an exact amount."""
    table = pa.table(
        {
            "id": pa.array(range(rows), pa.int64()),
            "amount": pa.array([Decimal(i) / 100 for i in range(rows)], pa.decimal128(18, 2)),
        }
    )
    return pa.RecordBatchReader.from_batches(table.schema, table.to_batches(per_batch)), table


def spilling(tmp_path: Any, **limits: Any) -> FederationLimits:
    return FederationLimits(spill_dir=str(tmp_path / "spill"), **limits)


class TestSpill:
    def test_a_small_leg_stays_in_memory(self, tmp_path: Any) -> None:
        spill = federation.QuerySpill(spilling(tmp_path))
        sink = federation.LegSink(leg(("Revenue", "decimal")), spill)
        reader, table = batches(5000)

        out = sink(reader)

        assert out.equals(table)
        assert not sink.spilled
        assert os.listdir(spill.directory) == []
        spill.close()

    def test_a_leg_past_the_threshold_streams_to_a_file_read_mapped(self, tmp_path: Any) -> None:
        spill = federation.QuerySpill(spilling(tmp_path, spill_threshold_bytes=16 * 1024))
        sink = federation.LegSink(leg(("Revenue", "decimal")), spill)
        reader, table = batches(50_000)

        out = sink(reader)

        assert out.equals(table)
        assert sink.spilled
        assert os.listdir(spill.directory) == ["leg_0.arrows"]
        # Readable by this process alone: spilled legs hold the warehouse's rows.
        assert os.stat(spill.directory).st_mode & 0o777 == 0o700
        spill.close()
        assert not os.path.exists(spill.directory)
        # The table outlives the file's name: its pages are mapped.
        assert out.equals(table)

    def test_a_dictionary_replaced_from_batch_to_batch_spills(self, tmp_path: Any) -> None:
        """ClickHouse sends ``LowCardinality`` with a dictionary per batch.

        The IPC file format refuses that ("Dictionary replacement detected"),
        so a leg spilled in it failed where the same leg in memory did not.
        """
        spill = federation.QuerySpill(spilling(tmp_path, spill_threshold_bytes=1024))
        sink = federation.LegSink(leg(("Region", "string")), spill)
        kind = pa.dictionary(pa.int32(), pa.string())
        pages = [
            pa.record_batch(
                [
                    pa.DictionaryArray.from_arrays(
                        pa.array([i % 2 for i in range(500)], pa.int32()),
                        pa.array([f"r{page}a", f"r{page}b"]),
                    )
                ],
                schema=pa.schema([("Region", kind)]),
            )
            for page in range(3)
        ]
        reader = pa.RecordBatchReader.from_batches(pages[0].schema, pages)

        out = sink(reader)

        assert sink.spilled
        assert out.schema.field("Region").type == kind
        assert (
            out.column("Region").to_pylist()
            == pa.Table.from_batches(pages).column("Region").to_pylist()
        )
        spill.close()

    def test_a_query_spilling_past_its_maximum_is_refused(self, tmp_path: Any) -> None:
        spill = federation.QuerySpill(
            spilling(tmp_path, spill_threshold_bytes=1024, spill_max_bytes=64 * 1024)
        )
        reader, _ = batches(50_000)

        with pytest.raises(FederatedLegError, match="FEDERATION_SPILL_TOO_LARGE"):
            federation.LegSink(leg(("Revenue", "decimal")), spill)(reader)
        spill.close()

    def test_queries_spilling_past_the_process_budget_are_refused_until_one_ends(
        self, tmp_path: Any
    ) -> None:
        limits = spilling(tmp_path, spill_threshold_bytes=1024, spill_budget_bytes=600 * 1024)
        first = federation.QuerySpill(limits)
        federation.LegSink(leg(("Revenue", "decimal")), first)(batches(20_000)[0])
        second = federation.QuerySpill(limits)

        with pytest.raises(federation.FederationSpillFullError, match="FEDERATION_SPILL_FULL"):
            federation.LegSink(leg(("Revenue", "decimal")), second)(batches(20_000)[0])
        second.close()
        first.close()

        third = federation.QuerySpill(limits)
        assert (
            federation.LegSink(leg(("Revenue", "decimal")), third)(batches(20_000)[0]).num_rows
            == 20_000
        )
        third.close()

    def test_a_disk_that_fills_while_spilling_is_named_not_blamed_on_the_database(
        self, tmp_path: Any
    ) -> None:
        import errno

        spill = federation.QuerySpill(spilling(tmp_path, spill_threshold_bytes=1024))
        full = OSError(errno.ENOSPC, "No space left on device")

        with (
            patch("pyarrow.OSFile", side_effect=full),
            pytest.raises(federation.FederationSpillFullError) as refused,
        ):
            federation.LegSink(leg(("Revenue", "decimal")), spill)(batches(20_000)[0])
        spill.close()

        assert "[FEDERATION_SPILL_FULL]" in str(refused.value)
        assert "No space left on device" in str(refused.value)
        assert "FEDERATION_SPILL_DIR" in str(refused.value)

    def test_a_spill_directory_that_cannot_be_written_is_named(self, tmp_path: Any) -> None:
        blocked = tmp_path / "file"
        blocked.write_text("")

        with pytest.raises(ExecutionUnavailableError, match="FEDERATION_SPILL_DIR"):
            federation.QuerySpill(FederationLimits(spill_dir=str(blocked / "spill")))


class TestSpilledQuery:
    async def test_spilled_legs_combine_to_what_one_source_returns(
        self, client: AsyncClient, tmp_path: Any
    ) -> None:
        single = await execute(client, single_model(), REVENUE_BY_REGION)
        federation.configure(spilling(tmp_path, spill_threshold_bytes=1))

        federated = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert federated.status_code == 200, federated.text
        assert federated.json()["rows"] == single.json()["rows"]
        assert federated.json()["columns"] == single.json()["columns"]
        # Every query's directory is gone once it ends.
        assert os.listdir(tmp_path / "spill") == []

    async def test_a_spilled_leg_is_not_cached_but_the_result_is(
        self, sources: dict[str, str], tmp_path: Any
    ) -> None:
        from orionbelt.api.query_cache import execute_federated_with_cache

        store, model_id, compiled = compiled_federated()
        cache = FileCache(
            cache_dir=str(tmp_path / "cache"),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=3600,
        )
        config = CacheRuntimeConfig(backend="file", min_ttl_seconds=1, unknown_policy="default_ttl")
        federation.configure(spilling(tmp_path, spill_threshold_bytes=1))

        await execute_federated_with_cache(
            store=store,
            model=store.get_model(model_id),
            compile_result=compiled,
            model_id=model_id,
            cache=cache,
            cache_config=config,
        )

        # The combined result alone: storing a spilled leg would read it all
        # back into memory.
        assert (await cache.stats()).entry_count == 1

    async def test_a_failing_query_removes_what_it_spilled(
        self, client: AsyncClient, tmp_path: Any
    ) -> None:
        federation.configure(spilling(tmp_path, spill_threshold_bytes=1, spill_max_bytes=1))

        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        assert response.status_code == 502
        assert "FEDERATION_SPILL_TOO_LARGE" in response.text
        assert os.listdir(tmp_path / "spill") == []
