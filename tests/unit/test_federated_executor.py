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

import threading
from collections.abc import AsyncIterator, Iterator
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
from orionbelt.compiler.federation import FederatedLeg, FederatedPlan  # noqa: E402
from orionbelt.service import federated_executor as federation  # noqa: E402
from orionbelt.service.db_executor import ExecutionResult  # noqa: E402
from orionbelt.service.federated_executor import (  # noqa: E402
    AdmissionGate,
    FederatedLegError,
    FederationLimits,
)
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402
from tests.unit.test_federation_planner import CUSTOMERS, ORDERS, model_yaml  # noqa: E402

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
        ("all", [ORDERS, CUSTOMERS]),
        ("shop", [ORDERS]),
        ("crm", [CUSTOMERS]),
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


def single_model() -> str:
    return model_yaml(federated=False).replace(
        "  defaultDialect: duckdb\n", "  defaultDialect: duckdb\n  defaultDataSource: all\n"
    )


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

    async def test_the_response_shows_where_each_leg_ran(self, client: AsyncClient) -> None:
        response = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)

        federation_info = response.json()["federation"]
        assert [leg["data_source"] for leg in federation_info["legs"]] == ["shop", "crm"]
        assert response.json()["dialect"] == "duckdb"

    async def test_each_leg_is_cached_on_its_own(self, client: AsyncClient) -> None:
        from orionbelt.api import query_cache

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
            again = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
            # A filter on the other source runs in the combine step, so the
            # legs' SQL is unchanged and both are still served from the cache.
            narrowed = await execute(client, model_yaml(federated=True), filtered)

        assert calls == ["shop", "crm"]
        assert again.json()["rows"] == first.json()["rows"]
        assert narrowed.json()["rows"] == [r for r in first.json()["rows"] if r[0] == "EU"]

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
        assert "FEDERATION_LEG_TOO_LARGE" in response.json()["detail"]
        assert "leg leg_0 on shop" in response.json()["detail"]

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


class TestNormalizeLeg:
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

        out = federation.leg_table(leg(("Order Date", "date"), ("Revenue", "decimal")), result)

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
