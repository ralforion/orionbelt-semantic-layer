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

        filtered = {
            **REVENUE_BY_REGION,
            "where": [{"field": "Region", "op": "equals", "value": "EU"}],
        }
        with patch.object(query_cache, "execute_sql", counting):
            first = await execute(client, model_yaml(federated=True), REVENUE_BY_REGION)
            # A filter on the other source runs in the combine step, so the
            # legs' SQL is unchanged and both are served from the cache.
            narrowed = await execute(client, model_yaml(federated=True), filtered)

        assert narrowed.json()["cached"] is False
        assert calls == ["shop", "crm"]
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
        assert "FEDERATION_LEG_TOO_LARGE" in response.json()["detail"]
        assert "leg leg_0 on shop" in response.json()["detail"]

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
