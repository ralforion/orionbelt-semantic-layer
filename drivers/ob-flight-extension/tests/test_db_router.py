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

    def test_dialects_without_aliases_are_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ob_flight.db_router import get_credentials

        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_PORT", "5432")
        creds = get_credentials("postgres")
        assert creds["host"] == "localhost"
        assert creds["port"] == 5432  # still coerced to int


class TestMotherDuckCredentials:
    """A ``md:`` database needs its token folded into the database string.

    ``duckdb.connect`` takes no token argument, so the token travels as a
    query parameter. Getting this wrong is quiet rather than loud: without a
    token the DuckDB extension falls back to interactive browser auth, which
    on a server hangs instead of failing.
    """

    @staticmethod
    def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "DUCKDB_DATABASE",
            "MOTHERDUCK_ACCESS_TOKEN",
            "MOTHERDUCK_TOKEN",
            "motherduck_token",
        ):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(
        "env_name", ["MOTHERDUCK_ACCESS_TOKEN", "MOTHERDUCK_TOKEN", "motherduck_token"]
    )
    def test_every_accepted_spelling_supplies_the_token(
        self, monkeypatch: pytest.MonkeyPatch, env_name: str
    ) -> None:
        from ob_flight.db_router import get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", "md:analytics")
        monkeypatch.setenv(env_name, "abc")
        assert get_credentials("duckdb")["database"] == "md:analytics?motherduck_token=abc"

    def test_token_is_url_encoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ob_flight.db_router import get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", "md:analytics")
        monkeypatch.setenv("MOTHERDUCK_ACCESS_TOKEN", "a/b+c=d")
        assert "motherduck_token=a%2Fb%2Bc%3Dd" in get_credentials("duckdb")["database"]

    @pytest.mark.parametrize("param", ["motherduck_token", "read_scaling_token"])
    def test_an_embedded_token_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, param: str
    ) -> None:
        """A read-scaling token is spelled differently and is still a token."""
        from ob_flight.db_router import get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", f"md:analytics?{param}=abc")
        assert get_credentials("duckdb")["database"] == f"md:analytics?{param}=abc"

    def test_local_file_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ob_flight.db_router import get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", "/tmp/local.duckdb")
        assert get_credentials("duckdb") == {"database": "/tmp/local.duckdb"}

    def test_tokenless_md_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ob_flight.db_router import MotherDuckTokenMissingError, get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", "md:analytics")
        with pytest.raises(MotherDuckTokenMissingError, match="MOTHERDUCK_ACCESS_TOKEN"):
            get_credentials("duckdb")

    def test_override_away_from_md_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The override contract: a caller-supplied database wins outright.

        Folding used to run before overrides were merged, so an env-configured
        tokenless ``md:`` raised even when the caller passed a local file.
        """
        from ob_flight.db_router import get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", "md:prod")
        assert get_credentials("duckdb", database=":memory:") == {"database": ":memory:"}

    def test_override_into_md_gets_the_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """And the reverse: an overridden ``md:`` must still be authenticated."""
        from ob_flight.db_router import get_credentials

        self._clear(monkeypatch)
        monkeypatch.setenv("DUCKDB_DATABASE", "/tmp/local.duckdb")
        monkeypatch.setenv("MOTHERDUCK_ACCESS_TOKEN", "T")
        creds = get_credentials("duckdb", database="md:analytics")
        assert creds["database"] == "md:analytics?motherduck_token=T"
