"""Track II-3: the ADBC auth matrix, driven through a real client.

Before this, none of it worked. ``SharedKeyAuthHandler`` implements Flight's
*legacy* ``Handshake``, and every current client - ADBC, and pyarrow's own
``authenticate_basic_token`` - uses the standard ``AuthenticateBasicToken``,
which carries the credential in an ``authorization`` header and expects the
issued token back in one. A handshake handler can do neither, so with
``AUTH_MODE=api_key`` every ADBC connection failed with "Stream is closed"
whether the key was right or wrong.

Each row here is a way a client actually sends a key, paired with its negative:
a mechanism that accepts the right key and also accepts the wrong one is worse
than one that accepts neither.
"""

# ruff: noqa: F811 — pytest fixtures are imported by name and then shadowed by
# the parameters that request them, which is how sharing one across modules
# works.
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.adbc_flight

from tests.conftest import SAMPLE_MODEL_YAML  # noqa: E402
from tests.integration.test_adbc_flightsql import (  # noqa: E402
    _SETUP_SQL,
    MODEL_NAME,
    conn,  # noqa: F401
    flight_uri,  # noqa: F401
)

#: Deliberately a length whose ``obsl:<key>`` base64 needs padding - ADBC
#: strips it, and a key that happens to encode padding-free would let the
#: decoder regress without a single test noticing.
API_KEY = "obsl-test-api-key-0123456789abcdefg"
WRONG_KEY = "obsl-test-api-key-wrong-00000000000"


@pytest.fixture(scope="module")
def authenticated_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A Flight server that requires the API key, as api_key mode configures."""
    pytest.importorskip("adbc_driver_flightsql")
    import duckdb
    from ob_flight.auth import AUTH_MIDDLEWARE_KEY, AuthMiddlewareFactory, NoopAuthHandler
    from ob_flight.server import OBFlightServer

    from orionbelt.service.session_manager import SessionManager

    db_path = tmp_path_factory.mktemp("adbc-auth") / "sample.duckdb"
    connection = duckdb.connect(str(db_path))
    connection.execute(_SETUP_SQL)
    connection.close()

    previous = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(db_path)

    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    manager.get_or_create_named(MODEL_NAME).load_model(SAMPLE_MODEL_YAML, dedup=False)
    server = OBFlightServer(
        "grpc://127.0.0.1:0",
        session_manager=manager,
        default_dialect="duckdb",
        auth_handler=NoopAuthHandler(),
        auth_middleware={AUTH_MIDDLEWARE_KEY: AuthMiddlewareFactory(lambda key: key == API_KEY)},
    )
    thread = threading.Thread(target=server.serve, name="adbc-auth-flight", daemon=True)
    thread.start()
    try:
        yield f"grpc://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        if previous is None:
            os.environ.pop("DUCKDB_DATABASE", None)
        else:
            os.environ["DUCKDB_DATABASE"] = previous


def _query(uri: str, **db_kwargs: Any) -> int:
    """Connect with the given credential and run a query. Returns the row count."""
    from adbc_driver_flightsql import dbapi

    connection = dbapi.connect(uri, **db_kwargs)
    try:
        with connection.cursor() as cur:
            cur.execute(f'SELECT "Customer Country" FROM {MODEL_NAME}')
            return int(cur.fetch_arrow_table().num_rows)
    finally:
        connection.close()


#: Every way a client sends the key, as ``(label, db_kwargs builder)``.
MECHANISMS = [
    ("basic", lambda key: {"db_kwargs": {"username": "obsl", "password": key}}),
    (
        "authorization_header",
        lambda key: {"db_kwargs": {"adbc.flight.sql.authorization_header": f"Bearer {key}"}},
    ),
    (
        "x-api-key",
        lambda key: {"db_kwargs": {"adbc.flight.sql.rpc.call_header.x-api-key": key}},
    ),
]


class TestTheAuthMatrix:
    @pytest.mark.parametrize(("label", "build"), MECHANISMS, ids=[m[0] for m in MECHANISMS])
    def test_the_right_key_is_accepted(
        self, authenticated_uri: str, label: str, build: Any
    ) -> None:
        assert _query(authenticated_uri, **build(API_KEY)) > 0

    @pytest.mark.parametrize(("label", "build"), MECHANISMS, ids=[m[0] for m in MECHANISMS])
    def test_the_wrong_key_is_refused(self, authenticated_uri: str, label: str, build: Any) -> None:
        """Paired with the row above on purpose: a mechanism that accepts the
        right key and also the wrong one is worse than one that accepts
        neither."""
        with pytest.raises(Exception, match="(?i)unauth|invalid|denied"):
            _query(authenticated_uri, **build(WRONG_KEY))

    def test_no_credential_is_refused(self, authenticated_uri: str) -> None:
        with pytest.raises(Exception, match="(?i)unauth|missing|invalid"):
            _query(authenticated_uri)

    def test_the_refusal_says_what_to_send(self, authenticated_uri: str) -> None:
        """A client that cannot connect should learn how from the error."""
        with pytest.raises(Exception) as raised:
            _query(authenticated_uri)
        message = str(raised.value).lower()
        assert "authorization" in message or "x-api-key" in message


class TestAnUnauthenticatedServerIsUnaffected:
    def test_no_credential_still_works(self, conn: Any) -> None:
        """The default surface takes no key; the middleware is only installed
        in api_key mode, so it costs nothing here."""
        with conn.cursor() as cur:
            cur.execute(f'SELECT "Customer Country" FROM {MODEL_NAME}')
            assert cur.fetch_arrow_table().num_rows > 0


class TestTheGuideDocumentsWhatWorks:
    """`docs/guide/adbc.md` lists the mechanisms; this list is what was measured.

    Documenting a fourth way to send a key, or dropping a row for one that
    works, should fail here rather than in a reader's terminal.
    """

    GUIDE = "docs/guide/adbc.md"

    def _documented_kwargs(self) -> list[frozenset[str]]:
        import ast
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parents[2] / self.GUIDE).read_text()
        section = re.search(r"### Authenticating\n(.*?)\n## ", text, re.S)
        assert section, f"no Authenticating section in {self.GUIDE}"
        rows = re.findall(r"^\|.*?\|\s*`(\{.*?\})`\s*\|$", section.group(1), re.M)
        return [frozenset(ast.literal_eval(row)) for row in rows]

    def test_the_table_was_found(self) -> None:
        """Guards the guard: a reformatted table would otherwise pass silently."""
        assert len(self._documented_kwargs()) > 1

    def test_every_documented_mechanism_is_one_the_matrix_proves(self) -> None:
        measured = {frozenset(build("k")["db_kwargs"]) for _, build in MECHANISMS}
        assert set(self._documented_kwargs()) == measured


class TestTheAuthenticationGuideRecipe:
    """`docs/guide/authentication.md` shows a raw pyarrow client, not ADBC.

    It was broken by the same cause - ``authenticate_basic_token`` is
    ``AuthenticateBasicToken`` - so it is worth proving separately rather than
    assuming the ADBC rows cover it.
    """

    def test_authenticate_basic_token_returns_a_usable_header(self, authenticated_uri: str) -> None:
        import pyarrow.flight as flight

        client = flight.FlightClient(authenticated_uri)
        token = client.authenticate_basic_token(b"token", API_KEY.encode())
        # The guide passes this pair straight into FlightCallOptions.
        assert token[0].lower() == b"authorization"
        options = flight.FlightCallOptions(headers=[token])
        list(client.list_flights(b"", options))

    def test_the_same_call_without_the_token_is_refused(self, authenticated_uri: str) -> None:
        import pyarrow.flight as flight

        client = flight.FlightClient(authenticated_uri)
        with pytest.raises(flight.FlightUnauthenticatedError):
            list(client.list_flights(b""))

    def test_a_wrong_key_never_yields_a_token(self, authenticated_uri: str) -> None:
        import pyarrow.flight as flight

        client = flight.FlightClient(authenticated_uri)
        with pytest.raises(flight.FlightUnauthenticatedError):
            client.authenticate_basic_token(b"token", WRONG_KEY.encode())
