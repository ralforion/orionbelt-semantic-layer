"""A query across data sources when one of them logs in as the user.

``shop`` holds the orders, a DuckDB file read with OBSL's own login. ``crm``
holds the customers on Databricks, logged in to as the caller
(``DB_CRM_AUTH=user``). Databricks is a stand-in: its token exchange answers
with a token naming the user, and its driver runs the leg's SQL (translated
from Databricks SQL) on that user's own copy of the customers, so a row-level
policy is a smaller copy: Bob sees only the EU customers.

The joined result must be each caller's own: the ``crm`` part as them, the
``shop`` part the same for all, cached apart per user. A BI token with a
warehouse sign-in runs it as its owner; a caller the ``crm`` source refuses is
refused before any part runs. See docs/guide/warehouse-sso/index.md, "Queries
across data sources".
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("ob_flight")
sqlglot = pytest.importorskip("sqlglot")

import jwt  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.api import query_cache  # noqa: E402
from orionbelt.api.app import create_app, oidc_config  # noqa: E402
from orionbelt.api.deps import (  # noqa: E402
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.auth import get_token_store, reset_auth  # noqa: E402
from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.service import user_login  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402
from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer  # noqa: E402
from tests.unit.test_federation_planner import CUSTOMERS, ORDERS, model_yaml  # noqa: E402

SERVICE_KEY = "obsl_pat_federation_user_login_key_012345"
TOKEN_AT = f"{ISSUER}/protocol/openid-connect/token"
DATABRICKS_AT = "https://dbc-1234.cloud.databricks.com/oidc/v1/token"
REVENUE_BY_REGION = {
    "select": {"dimensions": ["Region"], "measures": ["Revenue", "Orders Count"]},
    "orderBy": [{"field": "Region"}],
}


class Warehouse:
    """Databricks for the test: its token exchange, and a driver over each user's copy."""

    def __init__(self, issuer: FakeIssuer, customers: dict[str, str]) -> None:
        self.issuer = issuer
        self.customers = customers  # user -> the DuckDB file their grants show
        self.subjects: list[str] = []  # who each exchange was for
        self.connected_as: list[str] = []
        self.refreshes: list[str] = []

    def post(self, url: str, data: dict[str, str], timeout: float, auth: Any = None) -> Any:
        if url == DATABRICKS_AT:
            sub = jwt.decode(data["subject_token"], options={"verify_signature": False})["sub"]
            self.subjects.append(sub)
            return _answer({"access_token": f"dbx-{sub}", "expires_in": 3600})
        assert url == TOKEN_AT and data["grant_type"] == "refresh_token", url
        self.refreshes.append(data["refresh_token"])
        return _answer(
            {
                "access_token": self.issuer.token("alice", scope="obsl:write"),
                "refresh_token": data["refresh_token"],
                "expires_in": 300,
            }
        )

    def connect(self, **kwargs: Any) -> Any:
        user = kwargs["access_token"].removeprefix("dbx-")
        self.connected_as.append(user)
        con = duckdb.connect(self.customers[user], read_only=True)

        class Cursor:
            def execute(self, sql: str, *args: Any) -> None:
                self.result = con.execute(
                    sqlglot.transpile(sql, read="databricks", write="duckdb")[0]
                )
                self.description = self.result.description

            def fetch_arrow_table(self) -> Any:
                return self.result.fetch_arrow_table()

            def fetchall(self) -> list[tuple[Any, ...]]:
                return list(self.result.fetchall())

            def close(self) -> None:
                pass

        return SimpleNamespace(cursor=Cursor, close=con.close)


def _answer(body: dict[str, Any]) -> Any:
    return SimpleNamespace(status_code=200, json=lambda: body)


def _database(path: Path, *statements: str) -> str:
    con = duckdb.connect(str(path))
    for statement in statements:
        con.execute(statement)
    con.close()
    return str(path)


@pytest.fixture
def warehouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Warehouse]:
    from ob_flight import db_router

    eu_only = "DELETE FROM customers WHERE region IS DISTINCT FROM 'EU'"
    paths = {
        "shop": _database(tmp_path / "shop.duckdb", ORDERS),
        "all": _database(tmp_path / "all.duckdb", ORDERS, CUSTOMERS),
        "all_bob": _database(tmp_path / "all_bob.duckdb", ORDERS, CUSTOMERS, eu_only),
    }
    customers = {
        "alice": _database(tmp_path / "crm_alice.duckdb", CUSTOMERS),
        "bob": _database(tmp_path / "crm_bob.duckdb", CUSTOMERS, eu_only),
    }
    for variable in ("DUCKDB_DATABASE", "DUCKDB_DATABASE_PATH", "MOTHERDUCK_ACCESS_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OBSL_DATABASES", "shop,crm,all,all_bob")
    for name, path in paths.items():
        monkeypatch.setenv(f"DB_{name.upper()}_TYPE", "duckdb")
        monkeypatch.setenv(f"DB_{name.upper()}_DUCKDB_DATABASE", path)
    monkeypatch.setenv("DB_CRM_TYPE", "databricks")
    monkeypatch.setenv("DB_CRM_AUTH", "user")
    monkeypatch.setenv("DB_CRM_DATABRICKS_SERVER_HOSTNAME", "dbc-1234.cloud.databricks.com")

    stand_in = Warehouse(FakeIssuer(), customers)
    monkeypatch.setattr(user_login.httpx, "post", stand_in.post)
    monkeypatch.setitem(
        sys.modules, "fake_ob_databricks", SimpleNamespace(connect=stand_in.connect)
    )
    monkeypatch.setitem(db_router.VENDOR_MAP, "databricks", "fake_ob_databricks")
    user_login.reset_tokens()
    yield stand_in
    user_login.reset_tokens()
    db_router.close_all_pools()


@pytest.fixture
async def client(warehouse: Warehouse, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(warehouse.issuer.jwks()))
    settings = Settings(
        session_ttl_seconds=3600,
        session_cleanup_interval=9999,
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_file=str(jwks),
        oidc_token_endpoint=TOKEN_AT,
        oidc_client_id="obsl",
        oidc_client_secret="obsl-secret",
        token_store_path=str(tmp_path / "bi-tokens.sqlite"),
        token_store_key=base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    cache = FileCache(
        cache_dir=str(tmp_path / "cache"),
        max_value_bytes=10 * 1024 * 1024,
        max_disk_bytes=50 * 1024 * 1024,
        max_ttl_seconds=3600,
        sweep_interval_seconds=3600,
    )
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc=oidc_config(settings),
        query_execute_enabled=True,
        db_vendor="duckdb",
        cache=cache,
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
        await cache.shutdown()
        reset_session_manager()
        reset_auth()


@pytest.fixture
def legs_run(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The dialect of each query OBSL sent to a data source."""
    ran: list[str] = []
    real = query_cache.execute_sql

    def counting(sql: str, *, dialect: str, **kwargs: Any) -> Any:
        ran.append(dialect)
        return real(sql, dialect=dialect, **kwargs)

    monkeypatch.setattr(query_cache, "execute_sql", counting)
    return ran


def _user(warehouse: Warehouse, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {warehouse.issuer.token(sub, scope='obsl:write')}"}


def _on(source: str) -> str:
    """The model with both data objects on one DuckDB source: the expected answer."""
    return model_yaml(federated=False).replace(
        "  defaultDialect: duckdb\n", f"  defaultDialect: duckdb\n  defaultDataSource: {source}\n"
    )


async def _execute(client: AsyncClient, headers: dict[str, str], yaml: str) -> Any:
    session = await client.post("/v1/sessions", headers=headers)
    if session.status_code != 201:
        return session
    sid = session.json()["session_id"]
    loaded = await client.post(
        f"/v1/sessions/{sid}/models", json={"model_yaml": yaml}, headers=headers
    )
    assert loaded.status_code == 201, loaded.text
    return await client.post(
        f"/v1/sessions/{sid}/query/execute",
        json={"model_id": loaded.json()["model_id"], "query": REVENUE_BY_REGION},
        headers=headers,
    )


FEDERATED = model_yaml(federated=True)


async def test_each_user_gets_their_own_part_and_their_own_cache(
    client: AsyncClient, warehouse: Warehouse, legs_run: list[str]
) -> None:
    alice, bob = _user(warehouse, "alice"), _user(warehouse, "bob")

    mine = await _execute(client, alice, FEDERATED)
    assert mine.status_code == 200, mine.text
    assert mine.json()["federation"] is not None
    assert sorted(legs_run) == ["databricks", "duckdb"]  # one part on each source
    assert mine.json()["rows"] == (await _execute(client, alice, _on("all"))).json()["rows"]

    bobs = await _execute(client, bob, FEDERATED)
    assert bobs.status_code == 200, bobs.text
    assert bobs.json()["cached"] is False  # never Alice's joined rows
    assert bobs.json()["rows"] == (await _execute(client, bob, _on("all_bob"))).json()["rows"]
    assert bobs.json()["rows"] != mine.json()["rows"]

    # The orders part is the service login's, the same for both: every order
    # is counted for each of them; only the regions they may see differ.
    def total(rows: list[list[Any]]) -> int:
        return sum(int(row[2]) for row in rows)

    assert total(mine.json()["rows"]) == total(bobs.json()["rows"])
    assert {row[0] for row in bobs.json()["rows"]} <= {"EU", None}

    assert warehouse.subjects == ["alice", "bob"]
    assert warehouse.connected_as == ["alice", "bob"]
    again = await _execute(client, alice, FEDERATED)
    assert again.json()["cached"] is True  # Alice's own entry


async def test_a_bi_token_with_a_sign_in_runs_it_as_its_owner(
    client: AsyncClient, warehouse: Warehouse
) -> None:
    store = get_token_store()
    assert store is not None
    token, _ = store.create(
        owner_sub="alice",
        owner_email=None,
        label="Tableau",
        scopes=frozenset({"obsl:read", "obsl:write"}),
        days=90,
        refresh_token="rt-alice",
    )
    answer = await _execute(client, {"Authorization": f"Bearer {token}"}, FEDERATED)
    assert answer.status_code == 200, answer.text
    expected = await _execute(client, _user(warehouse, "alice"), _on("all"))
    assert answer.json()["rows"] == expected.json()["rows"]
    assert warehouse.refreshes == ["rt-alice"]
    assert warehouse.subjects == ["alice"]


@pytest.mark.parametrize("caller", ["service key", "BI token without a sign-in"])
async def test_a_caller_the_user_login_source_refuses_runs_no_part(
    client: AsyncClient, warehouse: Warehouse, legs_run: list[str], caller: str
) -> None:
    if caller == "service key":
        headers = {"X-API-Key": SERVICE_KEY}
    else:
        store = get_token_store()
        assert store is not None
        token, _ = store.create(
            owner_sub="alice",
            owner_email=None,
            label="Tableau",
            scopes=frozenset({"obsl:read", "obsl:write"}),
            days=90,
        )
        headers = {"Authorization": f"Bearer {token}"}
    refused = await _execute(client, headers, FEDERATED)
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"]["code"] == "SIGN_IN_REQUIRED"
    assert legs_run == []  # not even the orders part, which the caller could read
    assert warehouse.connected_as == []
