"""The Postgres wire protocol under AUTH_MODE=oidc, with psycopg (libpq) and real tokens.

A token is the password, accepted only over TLS (SCRAM cannot check a token);
an API key works with or without TLS. The connection runs as its caller, so
one user's session is not reachable by another over pgwire either.
"""

from __future__ import annotations

import asyncio
import threading
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
    bind_principal,
    init_auth,
    reset_auth,
    reset_principal,
)
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.session_manager import SessionManager
from orionbelt.service.tls import load_listener_tls
from tests.conftest import SAMPLE_MODEL_YAML
from tests.integration.test_pgwire_tls import _pki
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

SERVICE_KEY = "obsl_pat_pgwire_oidc_service_0123456789abcdef"


@pytest.fixture(autouse=True)
def no_home_root_certificate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # libpq reads ~/.postgresql/root.crt for sslmode=require; keep it out.
    monkeypatch.setenv("PGSSLROOTCERT", str(tmp_path / "absent.crt"))


@dataclass(frozen=True)
class Listener:
    port: int
    idp: FakeIssuer
    alice_session: str


def _serve(server: PgWireServer) -> Iterator[int]:
    """Run ``server`` on its own loop thread (psycopg blocks) and yield its port."""
    ready = threading.Event()
    bound: dict[str, int] = {}
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bound["port"] = loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="pgwire-oidc-test", daemon=True)
    thread.start()
    assert ready.wait(30), "pgwire listener did not start"
    try:
        yield bound["port"]
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def listener(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Listener]:
    idp = FakeIssuer()
    config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)
    init_auth(
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(idp)),
    )
    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    token = bind_principal(Principal(kind="user", sub="alice", scopes=frozenset({SCOPE_READ})))
    try:
        alice_session = manager.create_session().session_id
        manager.get_store(alice_session).load_model(SAMPLE_MODEL_YAML, dedup=False)
    finally:
        reset_principal(token)

    pki = _pki(tmp_path_factory.mktemp("pgwire-oidc"))
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        auth_mode="trust",  # the legacy default: SCRAM on plain connections
        tls=load_listener_tls(pki["cert"], pki["key"], prefix="PGWIRE"),
        query_handler=SemanticRouter(session_manager=manager, default_dialect="duckdb").handle,
    )
    try:
        for port in _serve(server):
            yield Listener(port, idp, alice_session)
    finally:
        reset_auth()


def _connect(port: int, password: str, *, sslmode: str, dbname: str = "x") -> Any:
    return psycopg.connect(
        host="127.0.0.1",
        port=port,
        user="obsl",
        password=password,
        dbname=dbname,
        sslmode=sslmode,
        connect_timeout=10,
    )


#: The tables a session holding the sample model lists, besides pgwire's own.
MODEL_TABLES = {"model", "dimensions", "measures", "metrics"}


def _model_tables(conn: Any) -> set[str]:
    """The tables the connection's session shows, without the system catalog."""
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables")
        return {row[0] for row in cur.fetchall() if not row[0].startswith("_")}


class TestLogin:
    def test_a_token_over_tls(self, listener: Listener) -> None:
        with _connect(listener.port, listener.idp.token("bob"), sslmode="require") as conn:
            assert conn.pgconn.ssl_in_use

    def test_a_token_without_tls_is_refused_and_says_why(self, listener: Listener) -> None:
        # Without TLS the server offers SCRAM, which a token cannot satisfy.
        with pytest.raises(psycopg.OperationalError, match="SCRAM authentication failed"):
            _connect(listener.port, listener.idp.token("bob"), sslmode="disable")

    def test_an_expired_token_is_refused_with_its_reason(self, listener: Listener) -> None:
        with pytest.raises(psycopg.OperationalError, match="Token has expired"):
            _connect(listener.port, listener.idp.token("bob", expires_in=-300), sslmode="require")

    def test_a_token_without_obsl_read_is_refused(self, listener: Listener) -> None:
        with pytest.raises(psycopg.OperationalError, match="requires obsl:read"):
            _connect(listener.port, listener.idp.token("bob", scope="openid"), sslmode="require")

    @pytest.mark.parametrize("sslmode", ["require", "disable"])
    def test_a_service_key_with_or_without_tls(self, listener: Listener, sslmode: str) -> None:
        with _connect(listener.port, SERVICE_KEY, sslmode=sslmode) as conn:
            assert conn.pgconn.ssl_in_use == (sslmode == "require")


class TestSessionOwnership:
    def test_the_owner_reads_their_session(self, listener: Listener) -> None:
        with _connect(
            listener.port,
            listener.idp.token("alice"),
            sslmode="require",
            dbname=listener.alice_session,
        ) as conn:
            assert _model_tables(conn) == MODEL_TABLES

    def test_another_user_does_not(self, listener: Listener) -> None:
        with _connect(
            listener.port,
            listener.idp.token("bob"),
            sslmode="require",
            dbname=listener.alice_session,
        ) as conn:
            # The session is not Bob's: his connection sees no model at all.
            assert _model_tables(conn) == set()
