"""A statement runs on the data source it is given, and is cached under it.

Named data sources (``ob_flight.datasources``) let one process know two
databases of one dialect. The executor, the result cache and Flight take the
source a query runs on; without one they use the dialect's implicit source,
as every connection did before, with the same cache keys.
"""

from __future__ import annotations

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
