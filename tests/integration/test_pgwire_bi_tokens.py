"""A BI token over the Postgres wire protocol, with psycopg (libpq).

Over TLS the token is a cleartext password, with any user name. Without TLS
the user name is the token's id and SCRAM checks the token against the
verifier stored with it, so the token never crosses the network.

The connection authenticates once, at startup. A BI token is checked again
before each query, so revoking it, or its expiry, ends a connection that is
already open, not only new ones: offboarding is not undone by a BI tool that
keeps its connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from orionbelt.auth import (
    SCOPE_READ,
    OidcConfig,
    Principal,
    bi_tokens,
    bind_principal,
    get_token_store,
    init_auth,
    reset_auth,
    reset_principal,
)
from orionbelt.auth.bi_tokens import TokenStore
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.session_manager import SessionManager
from orionbelt.service.tls import load_listener_tls
from tests.conftest import SAMPLE_MODEL_YAML
from tests.integration.test_pgwire_oidc import MODEL_TABLES, _serve
from tests.integration.test_pgwire_tls import _pki
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

TABLES = "SELECT table_name FROM information_schema.tables"


@pytest.fixture(autouse=True)
def no_home_root_certificate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # libpq reads ~/.postgresql/root.crt for sslmode=require; keep it out.
    monkeypatch.setenv("PGSSLROOTCERT", str(tmp_path / "absent.crt"))


@dataclass(frozen=True)
class Listener:
    port: int
    store: TokenStore
    alice_session: str


@pytest.fixture(scope="module")
def listener(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Listener]:
    state = tmp_path_factory.mktemp("pgwire-bi-tokens")
    config = OidcConfig(issuer=ISSUER, audience=AUDIENCE, token_store_path=str(state / "bi.sqlite"))
    init_auth(
        auth_mode="oidc",
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
    )
    store = get_token_store()
    assert store is not None
    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    token = bind_principal(Principal(kind="user", sub="alice", scopes=frozenset({SCOPE_READ})))
    try:
        alice_session = manager.create_session().session_id
        manager.get_store(alice_session).load_model(SAMPLE_MODEL_YAML, dedup=False)
    finally:
        reset_principal(token)
    pki = _pki(state)
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        auth_mode="trust",
        tls=load_listener_tls(pki["cert"], pki["key"], prefix="PGWIRE"),
        query_handler=SemanticRouter(session_manager=manager, default_dialect="duckdb").handle,
    )
    try:
        for port in _serve(server):
            yield Listener(port, store, alice_session)
    finally:
        reset_auth()


def _new_token(listener: Listener, days: int = 90) -> tuple[str, str]:
    token, record = listener.store.create(
        owner_sub="alice",
        owner_email="alice@example.com",
        label="Tableau",
        scopes=frozenset({SCOPE_READ}),
        days=days,
    )
    return token, record.id


def _connect(listener: Listener, token: str, *, user: str = "obsl", tls: bool = True) -> Any:
    # Autocommit, as BI tools run: otherwise psycopg sends a BEGIN (a simple
    # query) ahead of a prepared statement, and the extended path goes untested.
    return psycopg.connect(
        autocommit=True,
        host="127.0.0.1",
        port=listener.port,
        user=user,
        password=token,
        dbname=listener.alice_session,
        sslmode="require" if tls else "disable",
        connect_timeout=10,
    )


def _tables(conn: Any, *, prepared: bool) -> set[str]:
    """Run the query as a simple query, or as a prepared statement (Parse once, Bind each)."""
    if prepared:
        with conn.cursor() as cur:
            cur.execute(TABLES, prepare=True)
            rows = cur.fetchall()
    else:
        with psycopg.ClientCursor(conn) as cur:
            cur.execute(TABLES)
            rows = cur.fetchall()
    return {row[0] for row in rows if not row[0].startswith("_")}


@pytest.mark.parametrize("prepared", [False, True], ids=["simple", "prepared"])
def test_revoking_a_token_ends_its_open_connection(listener: Listener, prepared: bool) -> None:
    token, token_id = _new_token(listener)
    with _connect(listener, token) as conn:
        assert _tables(conn, prepared=prepared) == MODEL_TABLES
        assert _tables(conn, prepared=prepared) == MODEL_TABLES  # a prepared one reused
        assert listener.store.revoke(token_id, owner_sub="alice")
        with pytest.raises(psycopg.OperationalError, match="BI token was revoked"):
            _tables(conn, prepared=prepared)
        assert conn.closed
    with pytest.raises(psycopg.OperationalError, match="BI token was revoked"):
        _connect(listener, token)


def test_an_expired_token_ends_its_open_connection(
    listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = _new_token(listener, days=1)
    with _connect(listener, token) as conn:
        assert _tables(conn, prepared=False) == MODEL_TABLES
        later = bi_tokens.time.time() + 86401
        monkeypatch.setattr(bi_tokens.time, "time", lambda: later)
        with pytest.raises(psycopg.OperationalError, match="BI token expired"):
            _tables(conn, prepared=False)


def test_another_token_s_connection_is_not_touched(listener: Listener) -> None:
    kept, _ = _new_token(listener)
    revoked, revoked_id = _new_token(listener)
    with _connect(listener, kept) as conn, _connect(listener, revoked):
        listener.store.revoke(revoked_id, owner_sub="alice")
        assert _tables(conn, prepared=True) == MODEL_TABLES


class TestWithoutTLS:
    """SCRAM, with the token's id as the user name: the token never crosses the network."""

    @pytest.mark.parametrize("prefix", ["", "tok_"])
    def test_the_token_s_id_as_user_logs_in_as_its_owner(
        self, listener: Listener, prefix: str
    ) -> None:
        token, token_id = _new_token(listener)
        with _connect(listener, token, user=prefix + token_id, tls=False) as conn:
            assert not conn.pgconn.ssl_in_use
            assert _tables(conn, prepared=True) == MODEL_TABLES  # alice's session

    def test_a_wrong_token_and_an_unknown_id_look_the_same(self, listener: Listener) -> None:
        token, token_id = _new_token(listener)
        other, _ = _new_token(listener)
        for user, password in ((token_id, other), ("aaaaaaaaaaaa", token)):
            with pytest.raises(psycopg.OperationalError, match="BI token is not valid"):
                _connect(listener, password, user=user, tls=False)

    def test_another_user_name_is_no_token_login(self, listener: Listener) -> None:
        token, _ = _new_token(listener)
        with pytest.raises(psycopg.OperationalError, match="SCRAM authentication failed"):
            _connect(listener, token, user="obsl", tls=False)

    def test_a_revoked_token_is_refused_and_ends_its_open_connection(
        self, listener: Listener
    ) -> None:
        token, token_id = _new_token(listener)
        with _connect(listener, token, user=token_id, tls=False) as conn:
            listener.store.revoke(token_id, owner_sub="alice")
            with pytest.raises(psycopg.OperationalError, match="BI token was revoked"):
                _tables(conn, prepared=False)
        with pytest.raises(psycopg.OperationalError, match="BI token was revoked"):
            _connect(listener, token, user=token_id, tls=False)
