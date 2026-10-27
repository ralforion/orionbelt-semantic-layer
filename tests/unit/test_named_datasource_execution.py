"""A statement runs on the data source it is given, and is cached under it.

Named data sources (``ob_flight.datasources``) let one process know two
databases of one dialect. The executor, the result cache and Flight take the
source a query runs on; without one they use the dialect's implicit source,
as every connection did before, with the same cache keys.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("ob_flight")
duckdb = pytest.importorskip("duckdb")

from ob_flight.datasources import (  # noqa: E402
    DataSource,
    configured_datasources,
    implicit_datasource,
)

from orionbelt.cache import build_datasource_key, datasource_key_for  # noqa: E402
from orionbelt.service import db_executor  # noqa: E402
from orionbelt.service.db_executor import (  # noqa: E402
    ExecutionUnavailableError,
    execute_sql,
    explain_sql,
)


@pytest.fixture
def shops(tmp_path: Any) -> list[DataSource]:
    """Two DuckDB files, each a named source: shop-eu and shop-us."""
    for name in ("eu", "us"):
        con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
        con.execute(f"CREATE TABLE shop AS SELECT '{name}' AS region")
        con.close()
    return configured_datasources(
        {
            "OBSL_DATABASES": "shop-eu,shop-us",
            "DB_SHOP_EU_TYPE": "duckdb",
            "DB_SHOP_EU_DUCKDB_DATABASE": str(tmp_path / "eu.duckdb"),
            "DB_SHOP_US_TYPE": "duckdb",
            "DB_SHOP_US_DUCKDB_DATABASE": str(tmp_path / "us.duckdb"),
        }
    )


@pytest.fixture(autouse=True)
def _fresh_tz_cache() -> Any:
    db_executor._reset_db_tz_cache()
    yield
    db_executor._reset_db_tz_cache()


class TestExecution:
    def test_each_source_answers_from_its_own_database(self, shops: list[DataSource]) -> None:
        regions = [
            execute_sql("SELECT region FROM shop", dialect="duckdb", source=source).rows[0][0]
            for source in shops
        ]

        assert regions == ["eu", "us"]

    def test_explain_runs_on_the_source(self, shops: list[DataSource]) -> None:
        assert explain_sql("SELECT region FROM shop", dialect="duckdb", source=shops[1])

    def test_a_source_of_another_dialect_is_refused(self) -> None:
        source = implicit_datasource("postgres", {"POSTGRES_HOST": "db"})

        with pytest.raises(ExecutionUnavailableError, match="is postgres, but the query"):
            execute_sql("SELECT 1", dialect="duckdb", source=source)

    def test_without_a_source_the_dialects_variables_still_apply(
        self, shops: list[DataSource], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DUCKDB_DATABASE", shops[0].getenv("DUCKDB_DATABASE") or "")

        assert execute_sql("SELECT region FROM shop", dialect="duckdb").rows == [["eu"]]

    def test_the_session_timezone_is_kept_per_named_source(self, shops: list[DataSource]) -> None:
        for source in shops:
            execute_sql("SELECT 1", dialect="duckdb", source=source)

        assert {f"duckdb:{source.fingerprint()}" for source in shops} == db_executor._DB_TZ_DETECTED


class TestConnectionLimit:
    def test_a_source_with_no_connection_free_is_unavailable_not_failed(
        self, tmp_path: Any
    ) -> None:
        """A busy source is a 503 naming the setting to raise, not a 502
        sending the reader to look at the database."""
        from ob_flight import db_router

        con = duckdb.connect(str(tmp_path / "shop.duckdb"))
        con.execute("CREATE TABLE shop AS SELECT 'eu' AS region")
        con.close()
        (source,) = configured_datasources(
            {
                "OBSL_DATABASES": "shop",
                "DB_SHOP_TYPE": "duckdb",
                "DB_SHOP_DUCKDB_DATABASE": str(tmp_path / "shop.duckdb"),
                "DB_SHOP_MAX_CONNECTIONS": "1",
                "DB_SHOP_ACQUIRE_TIMEOUT_SECONDS": "0.05",
            }
        )
        db_router.close_all_pools()
        try:
            with (
                db_router.connection_slot(source),
                pytest.raises(ExecutionUnavailableError, match=r"\[DATA_SOURCE_BUSY\]"),
            ):
                execute_sql("SELECT region FROM shop", dialect="duckdb", source=source)

            assert execute_sql("SELECT region FROM shop", dialect="duckdb", source=source).rows == [
                ["eu"]
            ]
        finally:
            db_router.close_all_pools()

    def test_the_timezone_warm_up_does_not_wait_for_a_busy_source(self, tmp_path: Any) -> None:
        """A best-effort probe: it skips a source with no connection free and
        tries again later, rather than waiting out the acquire timeout."""
        import time

        from ob_flight import db_router

        (source,) = configured_datasources(
            {
                "OBSL_DATABASES": "shop",
                "DB_SHOP_TYPE": "duckdb",
                "DB_SHOP_DUCKDB_DATABASE": str(tmp_path / "shop.duckdb"),
                "DB_SHOP_MAX_CONNECTIONS": "1",
                "DB_SHOP_ACQUIRE_TIMEOUT_SECONDS": "5",
            }
        )
        duckdb.connect(str(tmp_path / "shop.duckdb")).close()
        key = f"duckdb:{source.fingerprint()}"
        db_router.close_all_pools()
        try:
            with db_router.connection_slot(source):
                started = time.monotonic()
                db_executor.warm_db_tz_cache("duckdb", source)
                assert time.monotonic() - started < 1
            assert key not in db_executor._DB_TZ_DETECTED

            db_executor.warm_db_tz_cache("duckdb", source)
            assert key in db_executor._DB_TZ_DETECTED
        finally:
            db_router.close_all_pools()


class TestSettingsEndpoint:
    async def test_the_timezone_warm_up_runs_off_the_event_loop(self) -> None:
        """/v1/settings probes the database on first use; the event loop, and
        every other request on it, keeps running while it does."""
        import asyncio
        import threading

        from httpx import ASGITransport, AsyncClient

        from orionbelt.api.app import create_app
        from orionbelt.api.deps import init_session_manager, reset_session_manager
        from orionbelt.service.session_manager import SessionManager
        from orionbelt.settings import Settings

        app = create_app(settings=Settings(session_ttl_seconds=3600, session_cleanup_interval=9999))
        mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
        init_session_manager(mgr, query_execute_enabled=True, db_vendor="postgres")
        probing = threading.Event()
        loop_ran = threading.Event()
        loop_ran_during_probe: list[bool] = []

        def probe(*_: Any) -> None:
            # Run on the loop's thread, this waits out its timeout: nothing
            # else on the loop can run to set the event.
            probing.set()
            loop_ran_during_probe.append(loop_ran.wait(5))

        async def tick(done: asyncio.Event) -> None:
            while not done.is_set():
                if probing.is_set():
                    loop_ran.set()
                await asyncio.sleep(0.005)

        try:
            with patch(
                "orionbelt.api.routers.settings.warm_db_tz_cache", side_effect=probe
            ) as warm:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    done = asyncio.Event()
                    ticker = asyncio.create_task(tick(done))
                    response = await client.get("/v1/settings")
                    done.set()
                    await ticker
            assert response.status_code == 200
            warm.assert_called_once()
            assert loop_ran_during_probe == [True]
        finally:
            reset_session_manager()
            mgr.stop()


class TestOlderDriver:
    def test_a_query_without_a_source_needs_no_data_source_api(
        self, shops: list[DataSource], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ob-flight-extension 2.36.0 on PyPI predates the data-source API and
        satisfies the requirement: a query without a source must run on what
        its router offers, get_credentials and get_connection by dialect."""
        import sys
        import types

        published = types.ModuleType("ob_flight.db_router")
        published.get_credentials = lambda dialect, **_: {  # type: ignore[attr-defined]
            "database": shops[0].getenv("DUCKDB_DATABASE")
        }
        monkeypatch.setitem(sys.modules, "ob_flight.db_router", published)

        assert execute_sql("SELECT region FROM shop", dialect="duckdb").rows == [["eu"]]


class TestCacheRace:
    async def test_a_source_changing_during_the_lookup_cannot_cache_other_rows(
        self, tmp_path: Any
    ) -> None:
        """Sources a and b share a fingerprint, and so a cache scope. a moves to
        another file while its cache lookup awaits: the query must still run on
        the settings its key came from, or b would be served a's new rows."""
        from orionbelt.api import query_cache

        for name in ("one", "two"):
            con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
            con.execute(f"CREATE TABLE shop AS SELECT '{name}' AS region")
            con.close()
        env = {
            "OBSL_DATABASES": "a,b",
            "DB_A_TYPE": "duckdb",
            "DB_B_TYPE": "duckdb",
            "DUCKDB_DATABASE": str(tmp_path / "one.duckdb"),
        }
        a, b = configured_datasources(env)

        async def lookup_while_a_moves(_cache: Any, _key: str) -> None:
            env["DB_A_DUCKDB_DATABASE"] = str(tmp_path / "two.duckdb")

        def plan(**kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                datasource=kwargs["datasource"],
                cache_key="k",
                ttl_outcome=SimpleNamespace(ttl=SimpleNamespace(seconds=60)),
            )

        stored: dict[str, Any] = {}

        async def store(**kwargs: Any) -> None:
            stored.update(kwargs)

        with (
            patch.object(query_cache, "resolve_cache_plan", side_effect=plan),
            patch.object(query_cache, "try_cache_get", side_effect=lookup_while_a_moves),
            patch.object(query_cache, "try_cache_set", side_effect=store),
            patch.object(query_cache, "build_result_columns", return_value=[]),
        ):
            result = await query_cache.execute_query_with_cache(
                store=None,
                model=None,
                compile_result=SimpleNamespace(
                    sql="SELECT region FROM shop", physical_tables=["shop"]
                ),
                model_id="m",
                dialect="duckdb",
                cache=MagicMock(),
                cache_config=MagicMock(),
                source=a,
            )

        assert result.exec_result.rows == [["one"]]
        assert stored["datasource"] == datasource_key_for(b)


class TestCacheScope:
    def test_an_implicit_source_keeps_the_existing_key(self) -> None:
        source = implicit_datasource("postgres", {"POSTGRES_HOST": "db"})

        assert datasource_key_for(source) == build_datasource_key("postgres") == "postgres"

    def test_named_sources_of_one_dialect_get_their_own_scope(
        self, shops: list[DataSource]
    ) -> None:
        eu, us = (datasource_key_for(source) for source in shops)

        assert eu != us
        assert eu == build_datasource_key("duckdb", shops[0].fingerprint())

    def test_two_names_for_one_target_share_a_scope(self) -> None:
        env = {"OBSL_DATABASES": "a,b", "DB_A_TYPE": "mysql", "DB_B_TYPE": "mysql"}
        a, b = configured_datasources({**env, "MYSQL_HOST": "db"})

        assert datasource_key_for(a) == datasource_key_for(b)


class TestFlight:
    def test_execution_connects_to_the_source(self, shops: list[DataSource]) -> None:
        from ob_flight import server_execution

        server = MagicMock()
        server._detect_virtual_table.return_value = None
        server._cache = None
        conn = MagicMock()
        conn.cursor.return_value.description = None

        with patch("ob_flight.server.db_connect", return_value=conn) as connect:
            server_execution.execute_sql(server, "SELECT 1", "duckdb", source=shops[1])

        connect.assert_called_once_with(shops[1])

    def test_execution_without_a_source_connects_by_dialect(self) -> None:
        from ob_flight import server_execution

        server = MagicMock()
        server._detect_virtual_table.return_value = None
        server._cache = None
        conn = MagicMock()
        conn.cursor.return_value.description = None

        with patch("ob_flight.server.db_connect", return_value=conn) as connect:
            server_execution.execute_sql(server, "SELECT 1", "duckdb")

        connect.assert_called_once_with("duckdb")

    def test_the_cache_key_is_scoped_to_the_source(self, shops: list[DataSource]) -> None:
        from ob_flight import server_execution

        server = MagicMock()
        server._cache.backend_name = "file"
        server._selector_from_context.return_value = "sales"
        plan = MagicMock()
        with patch(
            "orionbelt.service.query_execution.resolve_cache_plan", return_value=plan
        ) as resolve:
            server_execution.build_cache_meta(
                server,
                compiled_sql="SELECT 1",
                dialect="duckdb",
                context=None,
                physical_tables=[],
                source=shops[0],
            )

        assert resolve.call_args.kwargs["datasource"] == datasource_key_for(shops[0])

    def test_a_source_changing_between_planning_and_execution_cannot_cache_other_rows(
        self, tmp_path: Any
    ) -> None:
        """Flight keys the cache when it plans a query and runs it when the
        client fetches. Sources a and b share a target and a key; a moves to
        another file in between. The query must run on the settings its key
        came from, or b's cache entry would hold a's new rows."""
        from ob_flight import server_execution

        for name in ("one", "two"):
            con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
            con.execute(f"CREATE TABLE shop AS SELECT '{name}' AS region")
            con.close()
        env = {
            "OBSL_DATABASES": "a,b",
            "DB_A_TYPE": "duckdb",
            "DB_B_TYPE": "duckdb",
            "DUCKDB_DATABASE": str(tmp_path / "one.duckdb"),
        }
        a, b = configured_datasources(env)

        server = MagicMock()
        server._cache.backend_name = "file"
        server._selector_from_context.return_value = "sales"
        server._session_manager.get_store.return_value.list_models.return_value = [
            SimpleNamespace(model_id="m")
        ]
        server._detect_virtual_table.return_value = None
        server._cache_get_table.return_value = None
        server._batch_size = 100

        def plan(**kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                datasource=kwargs["datasource"],
                cache_key="k",
                ttl_outcome=SimpleNamespace(ttl=SimpleNamespace(seconds=60)),
            )

        with patch("orionbelt.service.query_execution.resolve_cache_plan", side_effect=plan):
            meta = server_execution.build_cache_meta(
                server,
                compiled_sql="SELECT region FROM shop",
                dialect="duckdb",
                context=None,
                physical_tables=["shop"],
                source=a,
            )
        env["DB_A_DUCKDB_DATABASE"] = str(tmp_path / "two.duckdb")

        server_execution.execute_sql(
            server, "SELECT region FROM shop", "duckdb", cache_meta=meta, source=a
        )

        assert meta is not None and meta["datasource"] == datasource_key_for(b)
        (stored, _meta), _ = server._cache_put_table.call_args
        assert stored.column("region").to_pylist() == ["one"]
