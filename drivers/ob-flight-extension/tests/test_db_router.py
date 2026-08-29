"""Tests for vendor database routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ob_flight.db_router import VENDOR_MAP, connect, get_credentials


class TestVendorMap:
    def test_all_dialects_present(self):
        expected = {
            "duckdb",
            "postgres",
            "snowflake",
            "clickhouse",
            "dremio",
            "databricks",
            "bigquery",
            "mysql",
        }
        assert set(VENDOR_MAP) == expected


class TestGetCredentials:
    def test_postgres_from_env(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.setenv("POSTGRES_USER", "admin")
        creds = get_credentials("postgres")
        assert creds["host"] == "db.example.com"
        assert creds["port"] == 5433  # converted to int
        assert creds["user"] == "admin"

    def test_snowflake_from_env(self, monkeypatch):
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "xy12345")
        monkeypatch.setenv("SNOWFLAKE_USER", "svc")
        creds = get_credentials("snowflake")
        assert creds["account"] == "xy12345"
        assert creds["user"] == "svc"

    def test_missing_env_vars_omitted(self, monkeypatch):
        # Clear all postgres env vars
        for key in [
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DBNAME",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
        ]:
            monkeypatch.delenv(key, raising=False)
        creds = get_credentials("postgres")
        assert creds == {}

    def test_unknown_dialect_returns_empty(self):
        creds = get_credentials("unknown")
        assert creds == {}

    def test_duckdb_database(self, monkeypatch):
        monkeypatch.setenv("DUCKDB_DATABASE", "/tmp/test.duckdb")
        creds = get_credentials("duckdb")
        assert creds["database"] == "/tmp/test.duckdb"


class TestConnect:
    def test_unsupported_dialect(self):
        with pytest.raises(KeyError, match="Unsupported dialect"):
            connect("oracle")

    def test_connect_duckdb(self, monkeypatch):
        monkeypatch.delenv("DUCKDB_DATABASE", raising=False)
        mock_module = MagicMock()
        mock_conn = MagicMock()
        mock_module.connect.return_value = mock_conn
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            result = connect("duckdb", database=":memory:")
            mock_import.assert_called_once_with("ob_duckdb")
            mock_module.connect.assert_called_once_with(database=":memory:", read_only=True)
            assert result is mock_conn

    def test_env_overridden_by_kwargs(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_HOST", "env-host")
        mock_module = MagicMock()
        with patch("importlib.import_module", return_value=mock_module):
            connect("postgres", host="override-host")
            call_kwargs = mock_module.connect.call_args.kwargs
            assert call_kwargs["host"] == "override-host"


class TestCredentialEnvAliases:
    """A canonical credential name may fall back to an alternate spelling.

    ``DATABRICKS_TOKEN`` is what Databricks' own CLI and SDK export, so it is
    what people already have set; ``.env.template`` and ``docs/drivers.md``
    document ``DATABRICKS_ACCESS_TOKEN``. Tests, the seed script and the probe
    script all accepted both — this router did not, so an environment written
    to the Databricks convention produced a connection with **no token** and
    nothing to explain why.
    """

    def test_alias_supplies_the_token_when_canonical_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ob_flight.db_router import get_credentials

        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "example.databricks.com")
        monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/x")
        monkeypatch.setenv("DATABRICKS_TOKEN", "from-alias")

        creds = get_credentials("databricks")
        assert creds["access_token"] == "from-alias"

    def test_canonical_wins_when_both_are_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ob_flight.db_router import get_credentials

        monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "example.databricks.com")
        monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/x")
        monkeypatch.setenv("DATABRICKS_TOKEN", "from-alias")
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "canonical")

        assert get_credentials("databricks")["access_token"] == "canonical"

    def test_no_token_set_yields_no_access_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ob_flight.db_router import get_credentials

        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "example.databricks.com")

        assert "access_token" not in get_credentials("databricks")

    def test_dialects_without_aliases_are_unaffected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ob_flight.db_router import get_credentials

        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_PORT", "5432")
        creds = get_credentials("postgres")
        assert creds["host"] == "localhost"
        assert creds["port"] == 5432  # still coerced to int
