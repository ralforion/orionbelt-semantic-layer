"""Data sources are configured by name, so one server can reach two databases of a kind.

Connections were global per dialect: one ``POSTGRES_HOST``, one pool, so a
server could not serve a CRM and a shop database both on Postgres. Named
sources are declared in the environment and read the dialect's variables
behind a ``DB_<NAME>_`` prefix. Ported from OrionBelt Analytics'
``test_named_databases.py``; the rules are the same so one ``.env`` serves both.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ob_flight import db_router
from ob_flight.datasources import (
    DataSource,
    DataSourceConfigError,
    DataSourceNotFoundError,
    configured_datasources,
    env_key,
    find_datasource,
    implicit_datasource,
)

SHOPS = {
    "OBSL_DATABASES": "shop-eu, shop-us",
    "DB_SHOP_EU_TYPE": "mysql",
    "DB_SHOP_EU_DESCRIPTION": "EU web shop",
    "DB_SHOP_EU_MYSQL_HOST": "eu.shop.internal",
    "DB_SHOP_US_TYPE": "mysql",
    "DB_SHOP_US_MYSQL_HOST": "us.shop.internal",
    "DB_SHOP_US_MYSQL_DATABASE": "shop_us",
    # Shared by both: the reporting user and the default database.
    "MYSQL_HOST": "shared.internal",
    "MYSQL_USER": "reporting",
    "MYSQL_PASSWORD": "s3cret-pw",
    "MYSQL_DATABASE": "shop",
}


class TestTheRegistry:
    def test_named_sources_come_in_the_order_listed(self) -> None:
        assert [s.name for s in configured_datasources(SHOPS)] == ["shop-eu", "shop-us"]

    def test_a_setting_falls_back_to_the_unprefixed_variable(self) -> None:
        eu, us = configured_datasources(SHOPS)

        assert eu.credentials() == {
            "host": "eu.shop.internal",
            "database": "shop",
            "user": "reporting",
            "password": "s3cret-pw",
        }
        assert us.credentials()["database"] == "shop_us"
        assert eu.getenv("MYSQL_PORT") is None

    def test_a_description_never_shows_credentials_or_hosts(self) -> None:
        described = [s.describe() for s in configured_datasources(SHOPS)]

        assert described == [
            {
                "name": "shop-eu",
                "dialect": "mysql",
                "description": "EU web shop",
                "database": "shop",
            },
            {"name": "shop-us", "dialect": "mysql", "database": "shop_us"},
        ]
        for leaked in ("s3cret-pw", "reporting", "shop.internal", "shared.internal"):
            assert leaked not in str(described)

    def test_a_dialect_configured_without_a_name_is_still_listed(self) -> None:
        (source,) = configured_datasources({"POSTGRES_HOST": "db", "POSTGRES_DBNAME": "crm"})

        assert (source.name, source.dialect, source.named) == ("postgres", "postgres", False)
        assert source.describe() == {"name": "postgres", "dialect": "postgres", "dbname": "crm"}

    def test_once_a_dialect_has_named_sources_its_variables_are_only_defaults(self) -> None:
        sources = configured_datasources({**SHOPS, "POSTGRES_HOST": "crm.internal"})

        assert [s.name for s in sources] == ["shop-eu", "shop-us", "postgres"]

    def test_nothing_configured_lists_nothing(self) -> None:
        assert configured_datasources({}) == []

    def test_names_match_the_way_people_write_them(self) -> None:
        sources = configured_datasources(SHOPS)

        for spoken in ("shop-eu", "Shop EU", "SHOP_EU"):
            assert find_datasource(spoken, sources).name == "shop-eu"
        with pytest.raises(DataSourceNotFoundError, match="Configured data sources: shop-eu"):
            find_datasource("crm", sources)

    def test_the_variable_prefix_is_derived_from_the_name(self) -> None:
        assert env_key("shop-eu") == "SHOP_EU"
        assert env_key("Sales EU / gold") == "SALES_EU_GOLD"
        (source,) = configured_datasources(
            {"OBSL_DATABASES": "shop-eu", "DB_SHOP_EU_TYPE": "mysql"}
        )
        assert source.prefix == "DB_SHOP_EU_"

    def test_the_analytics_type_spelling_is_accepted(self) -> None:
        env = {"OBSL_DATABASES": "crm", "DB_CRM_TYPE": "PostgreSQL"}

        assert configured_datasources(env)[0].dialect == "postgres"

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({"OBSL_DATABASES": "hr"}, "has no type"),
            ({"OBSL_DATABASES": "hr", "DB_HR_TYPE": "oracle"}, "unsupported type 'oracle'"),
            ({"OBSL_DATABASES": "hr,HR", "DB_HR_TYPE": "duckdb"}, "appears twice"),
            (
                {"OBSL_DATABASES": "sales.eu,sales/eu", "DB_SALES_EU_TYPE": "duckdb"},
                "would both read DB_SALES_EU_",
            ),
            ({"OBSL_DATABASES": "--"}, "no letters or digits"),
        ],
    )
    def test_a_broken_declaration_is_reported(self, env: dict[str, str], message: str) -> None:
        with pytest.raises(DataSourceConfigError, match=message):
            configured_datasources(env)


class TestSpellings:
    """Both products read one ``.env``: Analytics' variable names work here."""

    @pytest.mark.parametrize(
        ("dialect", "variable", "kwarg"),
        [
            ("postgres", "POSTGRES_DATABASE", "dbname"),
            ("postgres", "POSTGRES_USERNAME", "user"),
            ("mysql", "MYSQL_USERNAME", "user"),
            ("snowflake", "SNOWFLAKE_USERNAME", "user"),
            ("bigquery", "BIGQUERY_PROJECT_ID", "project"),
            ("bigquery", "BIGQUERY_CREDENTIALS_PATH", "credentials_file"),
            ("duckdb", "DUCKDB_DATABASE_PATH", "database"),
        ],
    )
    def test_an_analytics_spelling_supplies_the_setting(
        self, dialect: str, variable: str, kwarg: str
    ) -> None:
        assert implicit_datasource(dialect, {variable: "x"}).credentials()[kwarg] == "x"

    def test_the_canonical_spelling_wins(self) -> None:
        env = {"POSTGRES_DBNAME": "canonical", "POSTGRES_DATABASE": "alias"}

        assert implicit_datasource("postgres", env).credentials()["dbname"] == "canonical"

    def test_a_sources_own_alias_wins_over_a_shared_canonical(self) -> None:
        env = {
            "OBSL_DATABASES": "crm",
            "DB_CRM_TYPE": "postgres",
            "DB_CRM_POSTGRES_DATABASE": "crm",
            "POSTGRES_DBNAME": "shared",
        }

        assert configured_datasources(env)[0].credentials()["dbname"] == "crm"


class TestIdentity:
    """A pool and, later, a cache entry belong to who connects to what."""

    def test_two_names_for_one_target_share_a_fingerprint(self) -> None:
        env = {
            "OBSL_DATABASES": "a,b",
            "DB_A_TYPE": "postgres",
            "DB_B_TYPE": "postgres",
            "POSTGRES_HOST": "db",
            "POSTGRES_PASSWORD": "pw",
        }
        a, b = configured_datasources(env)

        assert a.fingerprint() == b.fingerprint()

    def test_a_changed_password_is_a_different_identity(self) -> None:
        before = implicit_datasource("postgres", {"POSTGRES_HOST": "db", "POSTGRES_PASSWORD": "1"})
        after = implicit_datasource("postgres", {"POSTGRES_HOST": "db", "POSTGRES_PASSWORD": "2"})

        assert before.fingerprint() != after.fingerprint()

    def test_the_same_settings_on_another_dialect_differ(self) -> None:
        env = {"MYSQL_HOST": "db", "POSTGRES_HOST": "db"}

        assert (
            implicit_datasource("mysql", env).fingerprint()
            != implicit_datasource("postgres", env).fingerprint()
        )

    def test_a_key_files_content_counts(self, tmp_path: Any) -> None:
        key = tmp_path / "key.json"
        key.write_text('{"client_email": "a@x"}')
        source = implicit_datasource("bigquery", {"BIGQUERY_CREDENTIALS_FILE": str(key)})
        before = source.fingerprint()
        key.write_text('{"client_email": "b@x"}')

        assert source.fingerprint() != before

    def test_the_fingerprint_shows_no_secret(self) -> None:
        source = implicit_datasource("postgres", {"POSTGRES_PASSWORD": "s3cret-pw"})

        assert len(source.fingerprint()) == 16
        assert "s3cret" not in source.fingerprint()
        assert "s3cret" not in repr(source)

    def test_a_source_keeps_its_own_pool_size(self) -> None:
        env = {"OBSL_DATABASES": "big", "DB_BIG_TYPE": "mysql", "DB_BIG_POOL_SIZE": "20"}

        assert configured_datasources(env)[0].pool_size() == 20
        assert implicit_datasource("mysql", {"DB_POOL_SIZE": "3"}).pool_size() == 3
        assert implicit_datasource("mysql", {}).pool_size() == 5


@pytest.fixture
def fresh_pools() -> Any:
    db_router.close_all_pools()
    yield
    db_router.close_all_pools()


class TestPools:
    def test_a_dialect_name_still_means_its_implicit_source(
        self, monkeypatch: pytest.MonkeyPatch, fresh_pools: None
    ) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
        module = MagicMock()
        with (
            patch("importlib.import_module", return_value=module),
            db_router.get_connection("postgres"),
        ):
            pass

        assert module.connect.call_args.kwargs["host"] == "db.example.com"

    def test_a_named_source_connects_with_its_own_settings(self, fresh_pools: None) -> None:
        eu, us = configured_datasources(SHOPS)
        module = MagicMock()
        with patch("importlib.import_module", return_value=module):
            with db_router.get_connection(eu):
                pass
            with db_router.get_connection(us):
                pass

        hosts = [c.kwargs["host"] for c in module.connect.call_args_list]
        assert hosts == ["eu.shop.internal", "us.shop.internal"]

    def test_one_target_under_two_names_reuses_the_connection(self, fresh_pools: None) -> None:
        env = {"OBSL_DATABASES": "a,b", "DB_A_TYPE": "mysql", "DB_B_TYPE": "mysql"}
        a, b = configured_datasources({**env, "MYSQL_HOST": "db"})
        module = MagicMock()
        with patch("importlib.import_module", return_value=module):
            with db_router.get_connection(a):
                pass
            with db_router.get_connection(b):
                pass

        assert module.connect.call_count == 1


class TestTwoDuckDBFiles:
    """Two files of one dialect, each its own source: the case the old router could not serve."""

    def test_each_source_reads_its_own_file(self, tmp_path: Any, fresh_pools: None) -> None:
        duckdb = pytest.importorskip("duckdb")
        pytest.importorskip("ob_duckdb")
        for name in ("eu", "us"):
            con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
            con.execute(f"CREATE TABLE shop AS SELECT '{name}' AS region")
            con.close()
        env = {
            "OBSL_DATABASES": "shop-eu,shop-us",
            "DB_SHOP_EU_TYPE": "duckdb",
            "DB_SHOP_EU_DUCKDB_DATABASE": str(tmp_path / "eu.duckdb"),
            "DB_SHOP_US_TYPE": "duckdb",
            "DB_SHOP_US_DUCKDB_DATABASE": str(tmp_path / "us.duckdb"),
        }

        regions = []
        for source in configured_datasources(env):
            assert isinstance(source, DataSource)
            with db_router.get_connection(source) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT region FROM shop")
                regions.append(cursor.fetchall()[0][0])

        assert regions == ["eu", "us"]

    def test_a_shared_pool_keeps_its_settings_when_one_source_moves(
        self, tmp_path: Any, fresh_pools: None
    ) -> None:
        """Two names share a pool while they point at one file. When the first
        moves to another file, the shared pool still serves the second its own
        file, on a new connection too."""
        duckdb = pytest.importorskip("duckdb")
        pytest.importorskip("ob_duckdb")
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
        with db_router.get_connection(a):
            pass  # the shared pool is created from a's settings
        env["DB_A_DUCKDB_DATABASE"] = str(tmp_path / "two.duckdb")

        regions = []
        with db_router.get_connection(b) as first, db_router.get_connection(b) as second:
            for conn in (first, second):  # the second is a new connection
                cursor = conn.cursor()
                cursor.execute("SELECT region FROM shop")
                regions.append(cursor.fetchall()[0][0])

        assert regions == ["one", "one"]
