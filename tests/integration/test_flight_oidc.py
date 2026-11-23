"""Arrow Flight SQL under AUTH_MODE=oidc, with a real ADBC client and real tokens.

The server is built with the auth the app installs (``build_shared_auth()``,
OBSL's own authenticate), the IdP is a stand-in whose keys are handed to the
verifier, and the client is ADBC. What it proves: a token logs in, a bad or
under-scoped one is refused with its reason, and a call runs as its caller, so
one user's session is not reachable by another over Flight either.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.adbc_flight

from orionbelt.auth import (  # noqa: E402
    SCOPE_READ,
    OidcConfig,
    Principal,
    bind_principal,
    init_auth,
    reset_auth,
    reset_principal,
)
from orionbelt.auth.jwt_verify import JwtVerifier  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from tests.conftest import SAMPLE_MODEL_YAML  # noqa: E402
from tests.integration.test_adbc_flightsql import _SETUP_SQL, MODEL_NAME  # noqa: E402
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer  # noqa: E402

SERVICE_KEY = "obsl_pat_flight_oidc_service_0123456789abcdef"


@dataclass(frozen=True)
class Server:
    uri: str
    idp: FakeIssuer
    alice_session: str


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    pytest.importorskip("adbc_driver_flightsql")
    import duckdb
    from ob_flight.auth import build_shared_auth
    from ob_flight.server import OBFlightServer

    idp = FakeIssuer()
    config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)
    init_auth(
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(idp)),
    )

    db_path = tmp_path_factory.mktemp("flight-oidc") / "sample.duckdb"
    connection = duckdb.connect(str(db_path))
    connection.execute(_SETUP_SQL)
    connection.close()
    previous = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(db_path)

    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    manager.get_or_create_named(MODEL_NAME).load_model(SAMPLE_MODEL_YAML, dedup=False)
    # Alice's own session, created as Alice (as REST would).
    token = bind_principal(Principal(kind="user", sub="alice", scopes=frozenset({SCOPE_READ})))
    try:
        alice_session = manager.create_session().session_id
        manager.get_store(alice_session).load_model(SAMPLE_MODEL_YAML, dedup=False)
    finally:
        reset_principal(token)

    handler, middleware = build_shared_auth()  # what app.py installs
    flight_server = OBFlightServer(
        "grpc://127.0.0.1:0",
        session_manager=manager,
        default_dialect="duckdb",
        auth_handler=handler,
        auth_middleware=middleware,
    )
    thread = threading.Thread(target=flight_server.serve, name="flight-oidc", daemon=True)
    thread.start()
    try:
        yield Server(f"grpc://127.0.0.1:{flight_server.port}", idp, alice_session)
    finally:
        flight_server.shutdown()
        thread.join(timeout=5)
        reset_auth()
        if previous is None:
            os.environ.pop("DUCKDB_DATABASE", None)
        else:
            os.environ["DUCKDB_DATABASE"] = previous


def _not_loaded(session_id: str) -> str:
    """The refusal for a session the caller may not see: the same as for none at all."""
    return rf"\[UNKNOWN_MODEL\] Model '{session_id}' is not loaded"


def _query(uri: str, bearer: str, *, model: str | None = None) -> int:
    """Run a query with ``Authorization: Bearer``; optionally select a session."""
    from adbc_driver_flightsql import dbapi

    db_kwargs = {"adbc.flight.sql.authorization_header": f"Bearer {bearer}"}
    if model is not None:
        db_kwargs["adbc.flight.sql.rpc.call_header.x-obsl-model"] = model
    connection = dbapi.connect(uri, db_kwargs=db_kwargs)
    try:
        with connection.cursor() as cur:
            # A session id is hex and may start with a digit: quote it.
            cur.execute(f'SELECT "Customer Country" FROM "{model or MODEL_NAME}"')
            return int(cur.fetch_arrow_table().num_rows)
    finally:
        connection.close()


class TestLogin:
    def test_a_token_logs_in(self, server: Server) -> None:
        assert _query(server.uri, server.idp.token("bob")) > 0

    def test_a_service_key_still_logs_in(self, server: Server) -> None:
        assert _query(server.uri, SERVICE_KEY) > 0

    def test_an_expired_token_is_refused_with_its_reason(self, server: Server) -> None:
        with pytest.raises(Exception, match="Token has expired"):
            _query(server.uri, server.idp.token("bob", expires_in=-300))

    def test_a_token_without_obsl_read_is_refused(self, server: Server) -> None:
        with pytest.raises(Exception, match="requires obsl:read"):
            _query(server.uri, server.idp.token("bob", scope="openid"))


class TestSessionOwnership:
    def test_the_owner_reaches_their_session(self, server: Server) -> None:
        assert _query(server.uri, server.idp.token("alice"), model=server.alice_session) > 0

    def test_another_user_does_not(self, server: Server) -> None:
        with pytest.raises(Exception, match=_not_loaded(server.alice_session)):
            _query(server.uri, server.idp.token("bob"), model=server.alice_session)

    def test_a_service_key_reaches_it(self, server: Server) -> None:
        assert _query(server.uri, SERVICE_KEY, model=server.alice_session) > 0


def test_the_caller_is_not_left_bound_on_the_server_threads(server: Server) -> None:
    # Bob's calls run as Bob, then Alice's own session is still hers alone.
    for _ in range(3):
        with pytest.raises(Exception, match=_not_loaded(server.alice_session)):
            _query(server.uri, server.idp.token("bob"), model=server.alice_session)
        assert _query(server.uri, server.idp.token("alice"), model=server.alice_session) > 0
