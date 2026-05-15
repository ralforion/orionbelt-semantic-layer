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
