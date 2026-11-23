"""A data source that logs in as the signed-in user (design/PLAN_sso_database_passthrough.md P1).

Databricks: the caller's IdP token is exchanged at the workspace for a
Databricks token, and each user's queries run on their own connection with
their own token. The token endpoint and the Databricks driver are stand-ins;
the configuration, exchange, connection, cache-scope and error paths are the
real ones.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("ob_flight")

from ob_flight import db_router  # noqa: E402
from ob_flight.datasources import (  # noqa: E402
    DataSource,
    DataSourceConfigError,
    configured_datasources,
)

from orionbelt.auth import SCOPE_READ, Principal, bind_principal, reset_principal  # noqa: E402
from orionbelt.auth.principal import API_KEY_PRINCIPAL  # noqa: E402
from orionbelt.cache import datasource_key_for  # noqa: E402
from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.cache.key import USER_SEP  # noqa: E402
from orionbelt.cache.ttl import NoCacheReason, TtlComputation, TtlResult  # noqa: E402
from orionbelt.service import user_login  # noqa: E402
from orionbelt.service.db_executor import execute_sql  # noqa: E402
from orionbelt.service.query_execution import cap_ttl  # noqa: E402
from orionbelt.service.user_login import (  # noqa: E402
    SignInRefusedError,
    SignInRequiredError,
    SignInUnavailableError,
)

HOST = "dbc-1234.cloud.databricks.com"
ENV = {
    "OBSL_DATABASES": "crm",
    "DB_CRM_TYPE": "databricks",
    "DB_CRM_AUTH": "user",
    "DB_CRM_DATABRICKS_SERVER_HOSTNAME": HOST,
    "DB_CRM_DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
}


def _user(sub: str) -> Principal:
    return Principal(kind="user", sub=sub, scopes=frozenset({SCOPE_READ}))


@contextmanager
def signed_in(sub: str, token: str | None = None) -> Iterator[None]:
    binding = bind_principal(_user(sub), token if token is not None else f"idp-token-of-{sub}")
    try:
        yield
    finally:
        reset_principal(binding)


@pytest.fixture
def crm() -> DataSource:
    (source,) = configured_datasources(ENV)
    return source


class TokenService:
    """Stands in for Databricks' /oidc/v1/token: answers per subject token."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.refuse: set[str] = set()
        self.status = 200
        self.down = False
        self.expires_in = 3600

    def post(self, url: str, data: dict[str, str], timeout: float) -> Any:
        self.requests.append((url, data))
        if self.down:
            raise httpx.ConnectError("unreachable")
        subject = data["subject_token"]
        if subject in self.refuse:
            return SimpleNamespace(
                status_code=400,
                json=lambda: {"error": "invalid_grant", "error_description": "no such user"},
            )
        if self.status >= 500:
            return SimpleNamespace(status_code=self.status, json=lambda: {"error": "server_error"})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": f"dbx:{subject}", "expires_in": self.expires_in},
        )


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[TokenService]:
    service = TokenService()
    monkeypatch.setattr(user_login.httpx, "post", service.post)
    user_login.reset_tokens()
    yield service
    user_login.reset_tokens()


class FakeDatabricks:
    """Stands in for ob_databricks: records each connection's arguments."""

    def __init__(self) -> None:
        self.connections: list[dict[str, Any]] = []
        self.closed = 0

    def connect(self, **kwargs: Any) -> Any:
        self.connections.append(kwargs)
        driver = self

        class Cursor:
            description = [("region", "string", None, None, None, None, None)]

            def execute(self, sql: str, *args: Any) -> None:
                self.rows = [("emea",)]

            def fetchall(self) -> list[tuple[str]]:
                return self.rows

            def fetchmany(self, size: int = 1) -> list[tuple[str]]:
                rows, self.rows = self.rows[:size], self.rows[size:]
                return rows

            def close(self) -> None:
                pass

        class Connection:
            def cursor(self) -> Cursor:
                return Cursor()

            def close(self) -> None:
                driver.closed += 1

        return Connection()


@pytest.fixture
def databricks(monkeypatch: pytest.MonkeyPatch) -> FakeDatabricks:
    fake = FakeDatabricks()
    monkeypatch.setitem(sys.modules, "fake_ob_databricks", SimpleNamespace(connect=fake.connect))
    monkeypatch.setitem(db_router.VENDOR_MAP, "databricks", "fake_ob_databricks")
    return fake


class TestConfiguration:
    def test_databricks_may_log_in_as_the_user(self, crm: DataSource) -> None:
        assert crm.login == "user"

    def test_a_dialect_that_cannot_is_refused(self) -> None:
        env = {**ENV, "DB_CRM_TYPE": "snowflake"}
        with pytest.raises(DataSourceConfigError, match="cannot log in as the user yet"):
            configured_datasources(env)

    def test_an_unknown_value_is_refused(self) -> None:
        with pytest.raises(DataSourceConfigError, match="DB_CRM_AUTH='users'"):
            configured_datasources({**ENV, "DB_CRM_AUTH": "users"})

    def test_a_service_login_keeps_its_fingerprint(self) -> None:
        # Existing named sources: same fingerprint, so the same pool and cache keys.
        service_env = {k: v for k, v in ENV.items() if k != "DB_CRM_AUTH"}
        (before,) = configured_datasources(service_env)
        (explicit,) = configured_datasources({**service_env, "DB_CRM_AUTH": "service"})
        (user,) = configured_datasources(ENV)
        assert before.fingerprint() == explicit.fingerprint()
        assert user.fingerprint() != before.fingerprint()


class TestTheExchange:
    def test_request_shape(self, crm: DataSource, tokens: TokenService) -> None:
        with signed_in("alice"):
            assert user_login.database_token(crm) == "dbx:idp-token-of-alice"
        url, form = tokens.requests[0]
        assert url == f"https://{HOST}/oidc/v1/token"
        assert form == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "idp-token-of-alice",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "scope": "all-apis",
        }

    def test_a_service_principal_policy_names_its_client(self, tokens: TokenService) -> None:
        (crm,) = configured_datasources({**ENV, "DB_CRM_OAUTH_CLIENT_ID": "sp-client"})
        with signed_in("alice"):
            user_login.database_token(crm)
        assert tokens.requests[0][1]["client_id"] == "sp-client"

    def test_another_client_is_another_identity(self, tokens: TokenService) -> None:
        """The client names the federation policy, which decides who the user is
        at the warehouse: two sources on one workspace that differ only in their
        client share no exchanged token and no cache scope."""
        env = {"OBSL_DATABASES": "admin,reader"}
        for name, client in (("ADMIN", "sp-admin"), ("READER", "sp-reader")):
            env |= {
                f"DB_{name}_TYPE": "databricks",
                f"DB_{name}_AUTH": "user",
                f"DB_{name}_DATABRICKS_SERVER_HOSTNAME": HOST,
                f"DB_{name}_DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
                f"DB_{name}_OAUTH_CLIENT_ID": client,
            }
        admin, reader = configured_datasources(env)
        assert admin.fingerprint() != reader.fingerprint()
        with signed_in("alice"):
            user_login.database_token(admin)
            user_login.database_token(reader)
            assert datasource_key_for(admin) != datasource_key_for(reader)
        assert [form["client_id"] for _, form in tokens.requests] == ["sp-admin", "sp-reader"]

    def test_a_token_is_reused_per_user_until_it_nears_expiry(
        self, crm: DataSource, tokens: TokenService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1000.0]
        monkeypatch.setattr(user_login.time, "monotonic", lambda: now[0])
        with signed_in("alice"):
            user_login.database_token(crm)
            user_login.database_token(crm)
        with signed_in("bob"):
            user_login.database_token(crm)
        assert len(tokens.requests) == 2  # one per user
        now[0] += 3600 - 59  # within the last minute
        with signed_in("alice"):
            user_login.database_token(crm)
        assert len(tokens.requests) == 3

    def test_a_refusal_names_the_reason_not_the_token(
        self, crm: DataSource, tokens: TokenService
    ) -> None:
        tokens.refuse.add("idp-token-of-mallory")
        with signed_in("mallory"), pytest.raises(SignInRefusedError) as excinfo:
            user_login.database_token(crm)
        assert "invalid_grant: no such user" in str(excinfo.value)
        assert "idp-token-of-mallory" not in str(excinfo.value)
        assert excinfo.value.status == 403

    @pytest.mark.parametrize("failure", ["down", "500"])
    def test_an_unreachable_or_failing_service_is_unavailable(
        self, crm: DataSource, tokens: TokenService, failure: str
    ) -> None:
        if failure == "down":
            tokens.down = True
        else:
            tokens.status = 500
        with signed_in("alice"), pytest.raises(SignInUnavailableError) as excinfo:
            user_login.database_token(crm)
        assert excinfo.value.status == 503

    def test_an_api_key_caller_is_no_user(self, crm: DataSource, tokens: TokenService) -> None:
        binding = bind_principal(API_KEY_PRINCIPAL)
        try:
            with pytest.raises(SignInRequiredError) as excinfo:
                user_login.database_token(crm)
        finally:
            reset_principal(binding)
        assert excinfo.value.status == 401
        assert tokens.requests == []


class TestConnections:
    def test_each_user_runs_on_their_own_token(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDatabricks
    ) -> None:
        for sub in ("alice", "bob"):
            with signed_in(sub):
                assert execute_sql("SELECT region", dialect="databricks", source=crm).rows
        assert [c["access_token"] for c in databricks.connections] == [
            "dbx:idp-token-of-alice",
            "dbx:idp-token-of-bob",
        ]

    def test_a_connection_is_never_pooled_for_the_next_caller(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDatabricks
    ) -> None:
        with signed_in("alice"):
            execute_sql("SELECT region", dialect="databricks", source=crm)
        with signed_in("bob"):
            execute_sql("SELECT region", dialect="databricks", source=crm)
        assert len(databricks.connections) == 2
        assert databricks.closed == 2
        assert db_router._get_pool(crm)._pool.qsize() == 0

    def test_an_api_key_caller_is_refused_unwrapped(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDatabricks
    ) -> None:
        # Passed through the executor as a sign-in error (401), not wrapped
        # into "Database execution failed" (502).
        binding = bind_principal(API_KEY_PRINCIPAL)
        try:
            with pytest.raises(SignInRequiredError):
                execute_sql("SELECT region", dialect="databricks", source=crm)
        finally:
            reset_principal(binding)
        assert databricks.connections == []


class TestCacheScope:
    def test_each_user_has_their_own_scope(self, crm: DataSource) -> None:
        with signed_in("alice"):
            alice = datasource_key_for(crm)
        with signed_in("bob"):
            bob = datasource_key_for(crm)
        base = alice.split(USER_SEP)[0]
        assert alice != bob
        assert alice.startswith(base + USER_SEP) and bob.startswith(base + USER_SEP)
        assert "alice" not in alice  # a hash, never the raw subject

    def test_no_user_no_key(self, crm: DataSource) -> None:
        with pytest.raises(SignInRequiredError):
            datasource_key_for(crm)

    def test_a_service_source_keeps_its_key(self) -> None:
        (service,) = configured_datasources({k: v for k, v in ENV.items() if k != "DB_CRM_AUTH"})
        assert USER_SEP not in datasource_key_for(service)

    async def test_dropping_a_data_source_drops_every_users_entries(self, tmp_path: Path) -> None:
        cache = FileCache(
            cache_dir=str(tmp_path),
            max_value_bytes=1024 * 1024,
            max_disk_bytes=10 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=60,
        )
        base = "databricks\x1fabc"
        scopes = [base, f"{base}{USER_SEP}u1", f"{base}{USER_SEP}u2", "databricks\x1fabcd"]
        try:
            for i, scope in enumerate(scopes):
                await cache.set(
                    f"k{i}",
                    b"x",
                    ttl_seconds=300,
                    physical_tables=[],
                    datasource=scope,
                    model_id="m",
                    query_hash="h",
                    dialect="databricks",
                    row_count=1,
                )
            assert await cache.delete_datasource(base) == 3
            assert await cache.get("k3") is not None  # another source sharing a prefix
        finally:
            await cache.shutdown()


class TestCacheTtl:
    def _outcome(self, seconds: int) -> TtlResult:
        return TtlResult(
            ttl=TtlComputation(
                seconds=seconds,
                derived_seconds=seconds,
                caller_seconds=None,
                source="freshness_derived",
                limiting_table=None,
                limiting_reason=None,
            )
        )

    def test_a_user_source_defaults_to_five_minutes(self, crm: DataSource) -> None:
        assert user_login.max_cache_ttl(crm) == 300

    def test_the_cap_is_a_setting(self) -> None:
        (crm,) = configured_datasources({**ENV, "DB_CRM_CACHE_MAX_TTL_SECONDS": "60"})
        assert user_login.max_cache_ttl(crm) == 60

    def test_a_service_source_has_no_cap(self) -> None:
        (service,) = configured_datasources({k: v for k, v in ENV.items() if k != "DB_CRM_AUTH"})
        assert user_login.max_cache_ttl(service) is None

    def test_cap_ttl(self) -> None:
        capped = cap_ttl(self._outcome(3600), 300, min_ttl_seconds=5)
        assert capped.ttl is not None
        assert (capped.ttl.seconds, capped.ttl.source) == (300, "user_login_capped")
        assert cap_ttl(self._outcome(60), 300, min_ttl_seconds=5).ttl.seconds == 60  # type: ignore[union-attr]

    def test_a_cap_below_the_minimum_means_no_cache(self) -> None:
        assert cap_ttl(self._outcome(3600), 0, min_ttl_seconds=5) == TtlResult(
            ttl=None, no_cache_reason=NoCacheReason.BELOW_MIN_TTL
        )
