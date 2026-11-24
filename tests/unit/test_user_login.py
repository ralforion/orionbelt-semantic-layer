"""A data source that logs in as the signed-in user (design/PLAN_sso_database_passthrough.md P1).

Databricks: the caller's IdP token is exchanged at the workspace for a
Databricks token, and each user's queries run on their own connection with
their own token. The token endpoint and the Databricks driver are stand-ins;
the configuration, exchange, connection, cache-scope and error paths are the
real ones.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
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

from orionbelt.auth import (  # noqa: E402
    SCOPE_READ,
    OidcClient,
    OidcConfig,
    Principal,
    bind_principal,
    get_oidc_client,
    init_auth,
    reset_auth,
    reset_principal,
)
from orionbelt.auth.jwt_verify import JwtVerifier  # noqa: E402
from orionbelt.auth.principal import API_KEY_PRINCIPAL  # noqa: E402
from orionbelt.cache import datasource_key_for  # noqa: E402
from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.cache.key import USER_SEP  # noqa: E402
from orionbelt.cache.ttl import NoCacheReason, TtlComputation, TtlResult  # noqa: E402
from orionbelt.service import user_login  # noqa: E402
from orionbelt.service.db_executor import execute_sql  # noqa: E402
from orionbelt.service.query_execution import cap_ttl  # noqa: E402
from orionbelt.service.user_login import (  # noqa: E402
    SignInInteractionRequiredError,
    SignInRefusedError,
    SignInRequiredError,
    SignInUnavailableError,
)
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer  # noqa: E402

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
    """Stands in for a token endpoint (Databricks', Entra's): answers per user token."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.refuse: set[str] = set()
        self.status = 200
        self.down = False
        self.expires_in = 3600
        self.error: dict[str, str] | None = None  # answered with HTTP 400 when set
        self.auths: list[tuple[str, str] | None] = []

    def post(
        self,
        url: str,
        data: dict[str, str],
        timeout: float,
        auth: tuple[str, str] | None = None,
    ) -> Any:
        self.requests.append((url, data))
        self.auths.append(auth)
        if self.down:
            raise httpx.ConnectError("unreachable")
        if self.error is not None:
            body = dict(self.error)
            return SimpleNamespace(status_code=400, json=lambda: body)
        # RFC 8693 names the user's token subject_token; Entra's OBO, assertion.
        subject = data.get("subject_token") or data["assertion"]
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


class FakeDriver:
    """Stands in for an ob_* driver: records each connection's arguments.

    ``ran_on`` names, per query, the token of the connection that ran it;
    ``fail`` is a query that fails.
    """

    def __init__(self) -> None:
        self.connections: list[dict[str, Any]] = []
        self.closed = 0
        self.ran_on: list[str] = []
        self.fail = "SELECT fails"

    def connect(self, **kwargs: Any) -> Any:
        self.connections.append(kwargs)
        driver = self
        token = kwargs.get("access_token") or kwargs.get("token") or ""

        class Cursor:
            description = [("region", "string", None, None, None, None, None)]

            def execute(self, sql: str, *args: Any) -> None:
                if sql == driver.fail:
                    raise RuntimeError("the warehouse failed this query")
                driver.ran_on.append(token)
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


@pytest.fixture(autouse=True)
def _no_idle_connections() -> Iterator[None]:
    """Pools outlive a test; an idle connection must not serve the next one."""
    yield
    db_router.close_all_pools()


@pytest.fixture
def databricks(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    fake = FakeDriver()
    monkeypatch.setitem(sys.modules, "fake_ob_databricks", SimpleNamespace(connect=fake.connect))
    monkeypatch.setitem(db_router.VENDOR_MAP, "databricks", "fake_ob_databricks")
    return fake


class TestConfiguration:
    def test_databricks_may_log_in_as_the_user(self, crm: DataSource) -> None:
        assert crm.login == "user"

    def test_a_dialect_that_cannot_is_refused(self) -> None:
        env = {**ENV, "DB_CRM_TYPE": "postgres"}
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
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        for sub in ("alice", "bob"):
            with signed_in(sub):
                assert execute_sql("SELECT region", dialect="databricks", source=crm).rows
        assert [c["access_token"] for c in databricks.connections] == [
            "dbx:idp-token-of-alice",
            "dbx:idp-token-of-bob",
        ]

    def test_a_connection_never_serves_another_user(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        for sub in ("alice", "bob", "alice", "bob"):
            with signed_in(sub):
                execute_sql("SELECT region", dialect="databricks", source=crm)
        assert len(databricks.connections) == 2  # one per user, each reused once
        assert databricks.ran_on == [
            "dbx:idp-token-of-alice",
            "dbx:idp-token-of-bob",
            "dbx:idp-token-of-alice",
            "dbx:idp-token-of-bob",
        ]
        assert db_router._get_pool(crm)._pool.qsize() == 0  # never the shared pool

    def test_an_api_key_caller_is_refused_unwrapped(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver
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


class TestConnectionReuse:
    """A user's idle connection serves that user's next query, within limits."""

    @staticmethod
    def _query(source: DataSource, sub: str, sql: str = "SELECT region") -> None:
        with signed_in(sub):
            execute_sql(sql, dialect="databricks", source=source)

    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        now = [1000.0]
        monkeypatch.setattr(user_login.time, "monotonic", lambda: now[0])  # the time module
        return now

    def test_the_next_query_reuses_the_connection(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        self._query(crm, "alice")
        self._query(crm, "alice")
        assert len(databricks.connections) == 1
        assert databricks.closed == 0
        assert len(tokens.requests) == 1

    def test_it_is_retired_before_its_token_expires(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver, clock: list[float]
    ) -> None:
        tokens.expires_in = 200  # usable for 140 s: a minute's margin
        self._query(crm, "alice")
        clock[0] += 150
        self._query(crm, "alice")
        assert len(databricks.connections) == 2
        assert databricks.closed == 1
        assert len(tokens.requests) == 2  # and the token was exchanged again

    def test_it_is_closed_after_the_idle_time(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver, clock: list[float]
    ) -> None:
        self._query(crm, "alice")
        clock[0] += 301  # USER_CONNECTION_IDLE_SECONDS defaults to 300
        self._query(crm, "alice")
        assert len(databricks.connections) == 2
        assert databricks.closed == 1

    def test_reuse_can_be_turned_off(self, tokens: TokenService, databricks: FakeDriver) -> None:
        (crm,) = configured_datasources({**ENV, "DB_CRM_USER_CONNECTION_IDLE_SECONDS": "0"})
        self._query(crm, "alice")
        self._query(crm, "alice")
        assert len(databricks.connections) == 2
        assert databricks.closed == 2

    def test_at_most_pool_size_are_kept_across_users(
        self, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        (crm,) = configured_datasources({**ENV, "DB_CRM_POOL_SIZE": "2"})
        for sub in ("alice", "bob", "carol"):
            self._query(crm, sub)
        assert databricks.closed == 1  # alice's, the least recently used
        self._query(crm, "alice")
        assert len(databricks.connections) == 4

    def test_a_failed_query_does_not_keep_its_connection(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        with pytest.raises(Exception, match="the warehouse failed"):
            self._query(crm, "alice", databricks.fail)
        assert databricks.closed == 1
        self._query(crm, "alice")
        assert len(databricks.connections) == 2

    def test_the_sweep_closes_an_expired_connection_without_a_query(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver, clock: list[float]
    ) -> None:
        self._query(crm, "alice")
        clock[0] += 301
        db_router.sweep_idle_user_connections()
        assert databricks.closed == 1

    def test_the_sweep_runs_by_itself_once_a_connection_is_kept(
        self,
        crm: DataSource,
        tokens: TokenService,
        databricks: FakeDriver,
        clock: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(db_router, "_SWEEP_INTERVAL_SECONDS", 0.01)
        self._query(crm, "alice")
        assert "obsl-user-connection-sweep" in {t.name for t in threading.enumerate()}
        clock[0] += 301
        deadline = time.monotonic_ns() + 2_000_000_000  # the real clock: 2 s
        while databricks.closed == 0 and time.monotonic_ns() < deadline:
            time.sleep(0.01)
        assert databricks.closed == 1

    @staticmethod
    def _two_names(second: dict[str, str]) -> tuple[DataSource, DataSource]:
        """Two names for one warehouse with the same credentials: one pool."""
        env = {"OBSL_DATABASES": "crm,crm2"}
        for name in ("CRM", "CRM2"):
            env |= {
                f"DB_{name}_TYPE": "databricks",
                f"DB_{name}_AUTH": "user",
                f"DB_{name}_DATABRICKS_SERVER_HOSTNAME": HOST,
                f"DB_{name}_DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
            }
        first, other = configured_datasources({**env, **second})
        assert first.fingerprint() == other.fingerprint()
        return first, other

    def test_a_source_that_keeps_none_never_reuses_a_shared_pool_s(
        self, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        crm, crm2 = self._two_names({"DB_CRM2_USER_CONNECTION_IDLE_SECONDS": "0"})
        self._query(crm, "alice")  # kept, under crm's 300 s
        self._query(crm2, "alice")  # crm2 keeps none: a connection of its own, closed
        assert len(databricks.connections) == 2
        assert databricks.closed == 1
        self._query(crm, "alice")  # crm's is still there
        assert len(databricks.connections) == 2

    def test_a_source_s_shorter_idle_time_holds_in_a_shared_pool(
        self, tokens: TokenService, databricks: FakeDriver, clock: list[float]
    ) -> None:
        crm, crm2 = self._two_names({"DB_CRM2_USER_CONNECTION_IDLE_SECONDS": "60"})
        self._query(crm, "alice")
        clock[0] += 120  # idle for 120 s: within crm's 300, past crm2's 60
        self._query(crm2, "alice")
        assert len(databricks.connections) == 2
        assert databricks.closed == 1
        clock[0] += 90  # crm2 kept it for 60 s: past that, even for crm
        db_router.sweep_idle_user_connections()
        assert databricks.closed == 2

    def test_shutdown_closes_the_idle_connections(
        self, crm: DataSource, tokens: TokenService, databricks: FakeDriver
    ) -> None:
        self._query(crm, "alice")
        self._query(crm, "bob")
        db_router.close_all_pools()
        assert databricks.closed == 2


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


# --- Snowflake: Entra ID issues the token, on behalf of the user ---------------

SNOWFLAKE_ENV = {
    "OBSL_DATABASES": "lake",
    "DB_LAKE_TYPE": "snowflake",
    "DB_LAKE_AUTH": "user",
    "DB_LAKE_OAUTH_EXCHANGE": "entra_obo",
    "DB_LAKE_OAUTH_SCOPE": "api://snowflake-app/session:role-any",
    "DB_LAKE_SNOWFLAKE_ACCOUNT": "xy12345",
    "DB_LAKE_SNOWFLAKE_WAREHOUSE": "BI_WH",
    "DB_LAKE_SNOWFLAKE_ROLE": "ANALYST",
    # A service login configured too: it must not go along with the user's token.
    "DB_LAKE_SNOWFLAKE_USER": "svc_obsl",
    "DB_LAKE_SNOWFLAKE_PASSWORD": "the-service-password",
}
CLIENT_SECRET = "the-obsl-client-secret"
TOKEN_ENDPOINT = "https://login.example.test/tenant-id/oauth2/v2.0/token"
CLAIMS = '{"access_token":{"polids":{"essential":true,"values":["policy-1"]}}}'


@contextmanager
def entra(
    token_endpoint: str = TOKEN_ENDPOINT, client_secret: str = CLIENT_SECRET
) -> Iterator[None]:
    """``AUTH_MODE=oidc`` with OBSL's own client at the identity provider."""
    config = OidcConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        token_endpoint=token_endpoint,
        client=OidcClient(client_id="obsl-app", client_secret=client_secret),
    )
    init_auth(
        auth_mode="oidc",
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
    )
    try:
        yield
    finally:
        reset_auth()


@pytest.fixture
def lake() -> DataSource:
    (source,) = configured_datasources(SNOWFLAKE_ENV)
    return source


@pytest.fixture
def snowflake(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    fake = FakeDriver()
    monkeypatch.setitem(sys.modules, "fake_ob_snowflake", SimpleNamespace(connect=fake.connect))
    monkeypatch.setitem(db_router.VENDOR_MAP, "snowflake", "fake_ob_snowflake")
    return fake


class TestSnowflake:
    @pytest.mark.parametrize(
        ("change", "match"),
        [
            ({"DB_LAKE_OAUTH_EXCHANGE": ""}, "OAUTH_EXCHANGE to entra_obo, token_exchange"),
            ({"DB_LAKE_OAUTH_EXCHANGE": "saml"}, "found 'saml'"),
            ({"DB_LAKE_OAUTH_SCOPE": ""}, "set DB_LAKE_OAUTH_SCOPE"),
        ],
    )
    def test_it_names_how_and_for_what(self, change: dict[str, str], match: str) -> None:
        with pytest.raises(DataSourceConfigError, match=match):
            configured_datasources({**SNOWFLAKE_ENV, **change})

    def test_the_scope_is_part_of_its_identity(self, lake: DataSource) -> None:
        """The scope carries the Snowflake role, and with it which rows come back."""
        (analyst,) = configured_datasources(
            {**SNOWFLAKE_ENV, "DB_LAKE_OAUTH_SCOPE": "api://snowflake-app/session:scope:analyst"}
        )
        assert lake.idp_issues_token
        assert lake.fingerprint() != analyst.fingerprint()

    def test_the_on_behalf_of_request(self, lake: DataSource, tokens: TokenService) -> None:
        with entra(), signed_in("alice"):
            assert user_login.database_token(lake) == "dbx:idp-token-of-alice"
        url, form = tokens.requests[0]
        assert url == TOKEN_ENDPOINT
        assert form == {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": "obsl-app",
            "client_secret": CLIENT_SECRET,
            "assertion": "idp-token-of-alice",
            "scope": "api://snowflake-app/session:role-any",
            "requested_token_use": "on_behalf_of",
        }

    def test_the_token_endpoint_is_discovered_unless_set(
        self, lake: DataSource, tokens: TokenService
    ) -> None:
        with entra(token_endpoint=""), signed_in("alice"):
            user_login.database_token(lake)
        assert tokens.requests[0][0] == FakeIssuer().discovery()["token_endpoint"]

    def test_conditional_access_asks_the_user_to_sign_in_again(
        self, lake: DataSource, tokens: TokenService
    ) -> None:
        tokens.error = {
            "error": "interaction_required",
            "error_description": "AADSTS50079: multi-factor authentication required",
            "claims": CLAIMS,
        }
        with entra(), signed_in("alice"), pytest.raises(SignInInteractionRequiredError) as raised:
            user_login.database_token(lake)
        assert raised.value.status == 401
        assert raised.value.claims == CLAIMS
        assert "AADSTS50079" in raised.value.message

    @pytest.mark.parametrize(
        ("error", "suberror"),
        [
            ("interaction_required", None),
            ("login_required", None),
            ("consent_required", None),
            # Entra marks an invalid_grant the user can resolve with a suberror.
            ("invalid_grant", "basic_action"),
            ("invalid_grant", "additional_action"),
            ("invalid_grant", "consent_required"),
        ],
    )
    def test_what_signing_in_again_resolves_is_not_a_refusal(
        self, lake: DataSource, tokens: TokenService, error: str, suberror: str | None
    ) -> None:
        tokens.error = {"error": error, "error_description": "AADSTS50079", "claims": CLAIMS}
        if suberror is not None:
            tokens.error["suberror"] = suberror
        with entra(), signed_in("alice"), pytest.raises(SignInInteractionRequiredError) as raised:
            user_login.database_token(lake)
        assert raised.value.claims == CLAIMS

    @pytest.mark.parametrize(
        ("error", "suberror"),
        [
            ("invalid_grant", None),
            ("invalid_grant", "client_mismatch"),
            ("invalid_grant", "protection_policy_required"),
            ("unauthorized_client", None),
        ],
    )
    def test_a_refusal_is_a_refusal(
        self, lake: DataSource, tokens: TokenService, error: str, suberror: str | None
    ) -> None:
        tokens.error = {"error": error, "error_description": "AADSTS50013"}
        if suberror is not None:
            tokens.error["suberror"] = suberror
        with entra(), signed_in("alice"), pytest.raises(SignInRefusedError, match="AADSTS50013"):
            user_login.database_token(lake)

    def test_the_connection_is_the_users_alone(
        self, lake: DataSource, tokens: TokenService, snowflake: FakeDriver
    ) -> None:
        with entra(), signed_in("alice"):
            assert execute_sql("SELECT region", dialect="snowflake", source=lake).rows
        (kwargs,) = snowflake.connections
        assert (kwargs["authenticator"], kwargs["token"]) == ("oauth", "dbx:idp-token-of-alice")
        assert kwargs["user"] is None
        assert kwargs["password"] is None
        assert (kwargs["account"], kwargs["warehouse"], kwargs["role"]) == (
            "xy12345",
            "BI_WH",
            "ANALYST",
        )

    def test_no_secret_reaches_a_log_line_or_a_message(
        self, lake: DataSource, tokens: TokenService, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        tokens.error = {"error": "invalid_grant", "error_description": "AADSTS50013"}
        with entra(), signed_in("alice", "alices-own-token"):
            with pytest.raises(SignInRefusedError) as refused:
                user_login.database_token(lake)
            tokens.error = None
            issued = user_login.database_token(lake)
            shown = [str(refused.value), repr(get_oidc_client())]
        shown += [record.getMessage() for record in caplog.records]
        for secret in (CLIENT_SECRET, "alices-own-token", issued):
            assert not [line for line in shown if secret in line], secret


# --- Snowflake: the standard token exchange at the identity provider ------------

TOKEN_EXCHANGE_ENV = {
    **SNOWFLAKE_ENV,
    "DB_LAKE_OAUTH_EXCHANGE": "token_exchange",
    "DB_LAKE_OAUTH_SCOPE": "session:role:ANALYST",
    "DB_LAKE_OAUTH_AUDIENCE": "snowflake",
}


class TestTokenExchangeAtTheIdentityProvider:
    """Okta, Keycloak and other RFC 8693 identity providers issue the Snowflake token."""

    def test_the_request(self, tokens: TokenService) -> None:
        (lake,) = configured_datasources(TOKEN_EXCHANGE_ENV)
        with entra(), signed_in("alice"):
            assert user_login.database_token(lake) == "dbx:idp-token-of-alice"
        url, form = tokens.requests[0]
        assert url == TOKEN_ENDPOINT
        assert form == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "idp-token-of-alice",
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": "session:role:ANALYST",
            "audience": "snowflake",
        }
        # The client authenticates with HTTP Basic, never in the form.
        assert tokens.auths == [("obsl-app", CLIENT_SECRET)]

    def test_the_basic_credentials_are_form_encoded(self, tokens: TokenService) -> None:
        """RFC 6749 2.3.1; checked against Keycloak, which refuses the raw secret."""
        (lake,) = configured_datasources(TOKEN_EXCHANGE_ENV)
        with entra(client_secret="a+b/c d%e"), signed_in("alice"):
            user_login.database_token(lake)
        assert tokens.auths == [("obsl-app", "a%2Bb%2Fc%20d%25e")]

    def test_no_audience_is_asked_for_unless_set(self, tokens: TokenService) -> None:
        env = {k: v for k, v in TOKEN_EXCHANGE_ENV.items() if k != "DB_LAKE_OAUTH_AUDIENCE"}
        (lake,) = configured_datasources(env)
        with entra(), signed_in("alice"):
            user_login.database_token(lake)
        assert "audience" not in tokens.requests[0][1]

    def test_the_audience_is_part_of_its_identity(self) -> None:
        (lake,) = configured_datasources(TOKEN_EXCHANGE_ENV)
        (other,) = configured_datasources({**TOKEN_EXCHANGE_ENV, "DB_LAKE_OAUTH_AUDIENCE": "other"})
        assert lake.fingerprint() != other.fingerprint()

    def test_entra_s_request_is_unchanged(self, lake: DataSource, tokens: TokenService) -> None:
        with entra(), signed_in("alice"):
            user_login.database_token(lake)
        assert tokens.auths == [None]  # Entra's on-behalf-of sends the secret in the form


# --- BigQuery: workforce identity federation at Google's token service --------

WORKFORCE_PROVIDER = "//iam.googleapis.com/locations/global/workforcePools/obsl/providers/idp"
BIGQUERY_ENV = {
    "OBSL_DATABASES": "bq",
    "DB_BQ_TYPE": "bigquery",
    "DB_BQ_AUTH": "user",
    "DB_BQ_BIGQUERY_PROJECT": "analytics-prod",
    "DB_BQ_WORKFORCE_PROVIDER": WORKFORCE_PROVIDER,
    # A service account key configured too: it must not serve the user's queries.
    "DB_BQ_BIGQUERY_CREDENTIALS_FILE": "/secrets/service-account.json",
}


@pytest.fixture
def bq() -> DataSource:
    (source,) = configured_datasources(BIGQUERY_ENV)
    return source


@pytest.fixture
def bigquery(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    fake = FakeDriver()
    monkeypatch.setitem(sys.modules, "fake_ob_bigquery", SimpleNamespace(connect=fake.connect))
    monkeypatch.setitem(db_router.VENDOR_MAP, "bigquery", "fake_ob_bigquery")
    return fake


class TestBigQuery:
    @pytest.mark.parametrize(
        ("change", "match"),
        [
            ({"DB_BQ_WORKFORCE_PROVIDER": ""}, "set DB_BQ_WORKFORCE_PROVIDER"),
            ({"DB_BQ_WORKFORCE_PROVIDER": "obsl/idp"}, "found 'obsl/idp'"),
            ({"DB_BQ_BIGQUERY_PROJECT": ""}, "billed for the token exchange"),
        ],
    )
    def test_it_names_its_workforce_provider(self, change: dict[str, str], match: str) -> None:
        with pytest.raises(DataSourceConfigError, match=match):
            configured_datasources({**BIGQUERY_ENV, **change})

    def test_the_exchange_is_billed_to_the_source_s_project_unless_set(
        self, bq: DataSource
    ) -> None:
        assert bq.workforce_user_project() == "analytics-prod"
        (other,) = configured_datasources({**BIGQUERY_ENV, "DB_BQ_WORKFORCE_USER_PROJECT": "quota"})
        assert other.workforce_user_project() == "quota"
        assert other.fingerprint() != bq.fingerprint()

    def test_the_provider_is_part_of_its_identity(self, bq: DataSource) -> None:
        (other,) = configured_datasources(
            {**BIGQUERY_ENV, "DB_BQ_WORKFORCE_PROVIDER": WORKFORCE_PROVIDER + "-2"}
        )
        assert other.fingerprint() != bq.fingerprint()

    def test_the_sts_request(self, bq: DataSource, tokens: TokenService) -> None:
        with signed_in("alice"):
            assert user_login.database_token(bq) == "dbx:idp-token-of-alice"
        url, form = tokens.requests[0]
        assert url == "https://sts.googleapis.com/v1/token"
        assert form == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": WORKFORCE_PROVIDER,
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": "idp-token-of-alice",
            "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
            "options": '{"userProject": "analytics-prod"}',
        }
        assert tokens.auths == [None]  # no client authenticates: no OBSL secret

    def test_a_refusal_is_a_refusal(self, bq: DataSource, tokens: TokenService) -> None:
        tokens.refuse.add("idp-token-of-alice")
        with signed_in("alice"), pytest.raises(SignInRefusedError, match="invalid_grant"):
            user_login.database_token(bq)

    def test_the_connection_is_the_users_alone(
        self, bq: DataSource, tokens: TokenService, bigquery: FakeDriver
    ) -> None:
        with signed_in("alice"):
            assert execute_sql("SELECT region", dialect="bigquery", source=bq).rows
        (kwargs,) = bigquery.connections
        assert kwargs["access_token"] == "dbx:idp-token-of-alice"
        assert kwargs["credentials_file"] is None  # the service account key stays unused
        assert kwargs["project"] == "analytics-prod"
