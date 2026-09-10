"""DuckDB as a client of the pgwire surface, through ``ATTACH ... (TYPE postgres)``.

This is the shape a data engineer actually wants: the semantic model appears
as a table in their own DuckDB, so it can be joined to local files and
persisted with ``CREATE TABLE AS`` without an OBSL-specific client library.

Everything here goes through DuckDB's own ``postgres`` extension, which talks
real Postgres wire protocol to a real listener. It is the only way to test
this: the extension's catalog enumeration is a single 20-line join across six
``pg_catalog`` relations, and a mistake anywhere in it returns *zero rows and
no error*. Every gap this file guards was found that way and none of them
raised anything.

The one thing a user must do is ``SET pg_use_text_protocol = true``. Without
it the extension reads data with ``COPY ... TO STDOUT (FORMAT binary)``, which
the semantic surface has no reason to implement - see
``test_binary_copy_is_the_setting_that_matters``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from orionbelt.auth import init_auth, reset_auth
from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.session_manager import SessionManager
from orionbelt.service.tls import load_listener_tls
from tests.conftest import SAMPLE_MODEL_YAML
from tests.integration.test_adbc_flightsql import _SETUP_SQL
from tests.integration.test_pgwire_tls import _pki

duckdb = pytest.importorskip("duckdb", reason="duckdb required to drive the client side")


@pytest.fixture(scope="module")
def pg_extension() -> Any:
    """A DuckDB connection with the ``postgres`` extension available.

    The extension is downloaded on first use, so an offline machine skips
    rather than fails - there is nothing wrong with the code in that case.
    """
    conn = duckdb.connect()
    try:
        conn.execute("INSTALL postgres")
        conn.execute("LOAD postgres")
    except Exception as exc:  # noqa: BLE001 - any failure here is environmental
        conn.close()
        pytest.skip(f"duckdb postgres extension unavailable: {exc}")
    conn.close()
    return True


@pytest.fixture(scope="module")
def pgwire_listener(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A pgwire listener backed by a real DuckDB warehouse, on its own loop thread.

    Own thread for the same reason the TLS suite needs one: the DuckDB client
    is blocking, so calling it from the loop's thread starves the accept.
    """
    db_path = tmp_path_factory.mktemp("pgwire-duckdb") / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_SETUP_SQL)
    # A customer who has never ordered: a dimension value with no facts behind
    # it. ``SELECT *`` does not return a row for it, so nothing else in this
    # file changes - but a row count taken over the dimensions alone does, and
    # that is exactly the bug ``test_count_star_agrees_with_select_star``
    # exists to catch.
    conn.execute("INSERT INTO PUBLIC.CUSTOMERS VALUES ('C3', 'DE')")
    conn.close()

    prev = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(db_path)

    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    manager.get_or_create_named("sales").load_model(SAMPLE_MODEL_YAML, dedup=False)
    router = SemanticRouter(session_manager=manager, default_dialect="duckdb")
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        auth_mode="trust",
        max_connections=16,
        query_handler=router.handle,
    )

    ready = threading.Event()
    bound: dict[str, int] = {}
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bound["port"] = loop.run_until_complete(server.start())
        ready.set()
        # ``run_forever``, not ``serve_forever``: ``start()`` is already
        # accepting connections, and stopping the loop out from under
        # ``serve_forever`` leaves its future pending - which surfaces as an
        # unraisable thread exception attributed to whichever test ran last.
        loop.run_forever()

    thread = threading.Thread(target=run, name="pgwire-duckdb-test", daemon=True)
    thread.start()
    assert ready.wait(30), "pgwire listener did not start"

    try:
        yield bound["port"]
    finally:
        # ``stop()`` first, then the loop: stopping the loop out from under
        # ``serve_forever`` leaves its future pending and pytest reports the
        # unraisable as a failure of whichever test happens to be last.
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        manager.stop()
        if prev is None:
            os.environ.pop("DUCKDB_DATABASE", None)
        else:
            os.environ["DUCKDB_DATABASE"] = prev


def _attach(port: int, *, text_protocol: bool = True) -> Any:
    conn = duckdb.connect()
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    if text_protocol:
        conn.execute("SET pg_use_text_protocol = true")
    conn.execute(
        f"ATTACH 'host=127.0.0.1 port={port} dbname=sales user=obsl' "
        f"AS obsl (TYPE postgres, READ_ONLY)"
    )
    return conn


@pytest.fixture
def attached(pg_extension: Any, pgwire_listener: int) -> Iterator[Any]:
    conn = _attach(pgwire_listener)
    try:
        yield conn
    finally:
        conn.close()


class TestTheCatalogEnumerates:
    def test_the_model_shows_up_as_a_table(self, attached: Any) -> None:
        """The assertion the ``pg_catalog`` work exists for.

        Before it this came back empty, with ATTACH reporting success.
        """
        rows = attached.execute(
            "SELECT schema, name FROM (SHOW ALL TABLES) WHERE database = 'obsl'"
        ).fetchall()
        assert ("sales", "model") in rows

    def test_columns_arrive_with_their_semantic_types(self, attached: Any) -> None:
        """A measure must not land as text; DuckDB reads the Postgres type OID."""
        types = dict(
            attached.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = 'model'"
            ).fetchall()
        )
        assert types["Customer Country"] == "VARCHAR"
        assert types["Total Revenue"] == "DOUBLE"
        assert types["Order Count"] == "BIGINT"


class TestQueryingTheModel:
    def test_by_name(self, attached: Any) -> None:
        rows = attached.execute(
            'SELECT "Customer Country", "Total Revenue" FROM obsl.sales.model ORDER BY 1'
        ).fetchall()
        assert rows == [("UK", 75.0), ("US", 150.0)]

    def test_a_filter(self, attached: Any) -> None:
        rows = attached.execute(
            'SELECT "Total Revenue" FROM obsl.sales.model WHERE "Customer Country" = \'US\''
        ).fetchall()
        assert rows == [(150.0,)]

    def test_joined_to_a_local_table(self, attached: Any) -> None:
        """The point of the whole exercise: the model composes with local data."""
        attached.execute("CREATE TABLE targets AS SELECT 'US' AS country, 120.0 AS target")
        rows = attached.execute(
            'SELECT m."Customer Country", m."Total Revenue" > t.target AS beat '
            'FROM obsl.sales.model m JOIN targets t ON m."Customer Country" = t.country'
        ).fetchall()
        assert rows == [("US", True)]

    def test_materialised_locally(self, attached: Any) -> None:
        attached.execute("CREATE TABLE snapshot AS SELECT * FROM obsl.sales.model")
        assert attached.execute("SELECT count(*) FROM snapshot").fetchone() == (2,)

    def test_count_star_agrees_with_select_star(self, attached: Any) -> None:
        """DuckDB sends this as ``SELECT NULL FROM model`` and counts the rows.

        Asserted against ``SELECT *`` rather than a literal, because agreeing
        with it is the whole requirement and a literal cannot express that. The
        warehouse holds a customer with no orders precisely so the two can
        disagree: answering the count from the dimensions alone drops the fact
        table from the query, and that customer becomes a third row that
        ``SELECT *`` never returns.
        """
        star = attached.execute("SELECT * FROM obsl.sales.model").fetchall()
        counted = attached.execute("SELECT count(*) FROM obsl.sales.model").fetchone()
        assert counted == (len(star),)
        assert counted == (2,), "the dangling dimension value must not be counted"

    def test_a_dimension_only_query_counts_something_else(self, attached: Any) -> None:
        """The asymmetry that makes the count above easy to get wrong.

        ``SELECT "Customer Country"`` alone needs no fact table, so it lists
        every country including the one with no orders - which is right, as a
        list of countries. ``SELECT *`` asks for measures too, which anchors
        the query to the facts and drops that country. Both are correct; they
        are simply not the same question, and a row count answered from the
        dimensions is answering the second one with the first.
        """
        dims_only = {
            row[0]
            for row in attached.execute(
                'SELECT "Customer Country" FROM obsl.sales.model'
            ).fetchall()
        }
        whole_model = {
            row[0] for row in attached.execute("SELECT * FROM obsl.sales.model").fetchall()
        }
        assert dims_only == {"UK", "US", "DE"}
        assert whole_model == {"UK", "US"}


def test_binary_copy_is_the_setting_that_matters(pg_extension: Any, pgwire_listener: int) -> None:
    """Without the text protocol, reads fail and the catalog still works.

    Pinned because the failure names ``COPY`` and a parse error, which reads
    like a broken query rather than a protocol the surface does not speak, and
    because it is the one line of setup a user has to be told about.
    """
    conn = _attach(pgwire_listener, text_protocol=False)
    try:
        rows = conn.execute("SELECT name FROM (SHOW ALL TABLES) WHERE database = 'obsl'").fetchall()
        assert ("model",) in rows

        with pytest.raises(Exception, match="(?i)copy"):
            conn.execute('SELECT "Customer Country" FROM obsl.sales.model').fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tls_listener(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[int, dict[str, str]]]:
    pytest.importorskip("cryptography", reason="cryptography required to mint test certs")
    out = tmp_path_factory.mktemp("pgwire-duckdb-tls")
    pki = _pki(out)

    db_path = out / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_SETUP_SQL)
    conn.close()

    prev = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(db_path)

    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    manager.get_or_create_named("sales").load_model(SAMPLE_MODEL_YAML, dedup=False)
    router = SemanticRouter(session_manager=manager, default_dialect="duckdb")
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        auth_mode="trust",
        max_connections=16,
        tls=load_listener_tls(pki["cert"], pki["key"], prefix="PGWIRE"),
        query_handler=router.handle,
    )

    ready = threading.Event()
    bound: dict[str, int] = {}
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bound["port"] = loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="pgwire-duckdb-tls-test", daemon=True)
    thread.start()
    assert ready.wait(30), "TLS pgwire listener did not start"
    try:
        yield bound["port"], pki
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        manager.stop()
        if prev is None:
            os.environ.pop("DUCKDB_DATABASE", None)
        else:
            os.environ["DUCKDB_DATABASE"] = prev


class TestOverTLS:
    """``ATTACH`` against a TLS listener, which needs nothing added to it.

    The extension is libpq underneath and passes the whole connection string
    through, so ``sslmode`` and ``sslrootcert`` work exactly as they do for
    ``psql``. Worth pinning rather than assuming: it is the difference between
    the surface being usable from DuckDB over an untrusted network and not.
    """

    def _read(self, port: int, extra: str) -> list[tuple[Any, ...]]:
        conn = duckdb.connect()
        try:
            conn.execute("INSTALL postgres")
            conn.execute("LOAD postgres")
            conn.execute("SET pg_use_text_protocol = true")
            conn.execute(
                f"ATTACH 'host=127.0.0.1 port={port} dbname=sales user=obsl {extra}' "
                f"AS obsl (TYPE postgres, READ_ONLY)"
            )
            return conn.execute(
                'SELECT "Customer Country", "Total Revenue" FROM obsl.sales.model ORDER BY 1'
            ).fetchall()
        finally:
            conn.close()

    @pytest.mark.parametrize("sslmode", ["disable", "prefer", "require"])
    def test_the_modes_that_need_no_certificate(
        self, pg_extension: Any, tls_listener: tuple[int, dict[str, str]], sslmode: str
    ) -> None:
        port, _ = tls_listener
        assert self._read(port, f"sslmode={sslmode}") == [("UK", 75.0), ("US", 150.0)]

    @pytest.mark.parametrize("sslmode", ["verify-ca", "verify-full"])
    def test_the_modes_that_verify(
        self, pg_extension: Any, tls_listener: tuple[int, dict[str, str]], sslmode: str
    ) -> None:
        port, pki = tls_listener
        extra = f"sslmode={sslmode} sslrootcert={pki['ca']}"
        assert self._read(port, extra) == [("UK", 75.0), ("US", 150.0)]

    def test_the_wrong_trust_anchor_is_refused(
        self, pg_extension: Any, tls_listener: tuple[int, dict[str, str]]
    ) -> None:
        """What makes the five above mean something: verification is real."""
        port, pki = tls_listener
        with pytest.raises(Exception, match="(?i)certificate|ssl"):
            self._read(port, f"sslmode=verify-ca sslrootcert={pki['other_ca']}")


# A well-formed key: ``init_auth`` refuses short or low-entropy ones, because a
# weak key is attackable offline from a captured SCRAM transcript.
_API_KEY = "obsl_pat_" + "a3f9" * 10


@pytest.fixture
def api_key_auth() -> Iterator[str]:
    reset_auth()
    init_auth(auth_mode="api_key", api_keys=_API_KEY)
    try:
        yield _API_KEY
    finally:
        reset_auth()


@contextlib.contextmanager
def _listener(auth_mode: str) -> Iterator[int]:
    """A listener in *auth_mode*, yielding its port. Shared by the auth tests."""
    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    manager.get_or_create_named("sales").load_model(SAMPLE_MODEL_YAML, dedup=False)
    router = SemanticRouter(session_manager=manager, default_dialect="duckdb")
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        auth_mode=auth_mode,
        max_connections=16,
        query_handler=router.handle,
    )
    ready = threading.Event()
    bound: dict[str, int] = {}
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bound["port"] = loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name=f"pgwire-{auth_mode}-test", daemon=True)
    thread.start()
    assert ready.wait(30), "pgwire listener did not start"
    try:
        yield bound["port"]
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        manager.stop()


class TestAuthentication:
    """The API key is the password, and DuckDB has nothing special to do.

    ``AUTH_MODE=api_key`` makes the listener demand a credential; the mechanism
    is SCRAM-SHA-256 unless an operator opts down to cleartext. Both are libpq's
    to perform, so ``password=<key>`` in the connection string is the whole of
    the client side - which is worth pinning, because SCRAM is the default and
    a client that could not do it would be locked out of an authenticated
    deployment entirely.
    """

    @pytest.fixture
    def warehouse(self, tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
        db_path = tmp_path / "warehouse.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(_SETUP_SQL)
        conn.close()
        prev = os.environ.get("DUCKDB_DATABASE")
        os.environ["DUCKDB_DATABASE"] = str(db_path)
        try:
            yield db_path
        finally:
            if prev is None:
                os.environ.pop("DUCKDB_DATABASE", None)
            else:
                os.environ["DUCKDB_DATABASE"] = prev

    def _read(self, port: int, extra: str) -> list[tuple[Any, ...]]:
        conn = duckdb.connect()
        try:
            conn.execute("INSTALL postgres")
            conn.execute("LOAD postgres")
            conn.execute("SET pg_use_text_protocol = true")
            conn.execute(
                f"ATTACH 'host=127.0.0.1 port={port} dbname=sales user=obsl {extra}' "
                f"AS obsl (TYPE postgres, READ_ONLY)"
            )
            return conn.execute('SELECT "Customer Country" FROM obsl.sales.model').fetchall()
        finally:
            conn.close()

    @pytest.mark.parametrize("auth_mode", ["scram", "password"])
    def test_the_api_key_is_the_password(
        self,
        pg_extension: Any,
        api_key_auth: str,
        warehouse: pathlib.Path,
        auth_mode: str,
    ) -> None:
        """``scram`` is the default; ``password`` is the cleartext opt-in."""
        with _listener(auth_mode) as port:
            assert len(self._read(port, f"password={api_key_auth}")) == 2

    @pytest.mark.parametrize("auth_mode", ["scram", "password"])
    def test_a_wrong_key_is_refused(
        self,
        pg_extension: Any,
        api_key_auth: str,
        warehouse: pathlib.Path,
        auth_mode: str,
    ) -> None:
        with (
            _listener(auth_mode) as port,
            pytest.raises(Exception, match="(?i)authentication failed|invalid api key"),
        ):
            self._read(port, "password=wrong")

    def test_no_password_is_refused(
        self, pg_extension: Any, api_key_auth: str, warehouse: pathlib.Path
    ) -> None:
        """Refused - by whichever side gets there first.

        Some libpq builds decline to send an empty credential for SCRAM at all
        (``fe_sendauth: no password supplied``) rather than letting the server
        answer; others complete the exchange and are rejected. Both are the
        same outcome, and which one happens is a property of the client build
        rather than of this surface. Asserting only the server's message passed
        locally and failed on CI.
        """
        with (
            _listener("scram") as port,
            pytest.raises(Exception, match="(?i)authentication failed|no password supplied"),
        ):
            self._read(port, "")


#: The sample model with its revenue measure declared as a fixed-scale decimal.
#: Every measure in the default fixture is a float, which is why the typmod
#: defect below shipped: no test in the suite produced a NUMERIC column, and a
#: real model almost always has one.
DECIMAL_MODEL_YAML = SAMPLE_MODEL_YAML.replace(
    """  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum""",
    """  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    dataType: decimal(18,2)
    aggregation: sum""",
)


@pytest.fixture(scope="module")
def decimal_listener(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A listener whose model declares a DECIMAL measure."""
    db_path = tmp_path_factory.mktemp("pgwire-duckdb-decimal") / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_SETUP_SQL)
    conn.close()

    prev = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(db_path)

    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    manager.get_or_create_named("sales").load_model(DECIMAL_MODEL_YAML, dedup=False)
    router = SemanticRouter(session_manager=manager, default_dialect="duckdb")
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        auth_mode="trust",
        max_connections=16,
        query_handler=router.handle,
    )

    ready = threading.Event()
    bound: dict[str, int] = {}
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bound["port"] = loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="pgwire-decimal-test", daemon=True)
    thread.start()
    assert ready.wait(30), "pgwire listener did not start"
    try:
        yield bound["port"]
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        manager.stop()
        if prev is None:
            os.environ.pop("DUCKDB_DATABASE", None)
        else:
            os.environ["DUCKDB_DATABASE"] = prev


class TestADecimalMeasure:
    """A governed DECIMAL has to survive the wire, and it did not.

    ``atttypmod`` reached the client in DuckDB's encoding rather than
    Postgres's, so the extension decoded ``DECIMAL(18, 2)`` as
    ``DECIMAL(0, 78)`` and then could not fit a single real value into it. The
    catalog browsed perfectly; only reading failed, with a conversion error
    naming a string that was entirely valid::

        Could not convert string "1936466.31" to DECIMAL(0,78)

    Nothing in the suite had a decimal measure before this, which is why it
    shipped. Most real models have one.
    """

    @pytest.fixture
    def attached_decimal(self, pg_extension: Any, decimal_listener: int) -> Iterator[Any]:
        conn = _attach(decimal_listener)
        try:
            yield conn
        finally:
            conn.close()

    def test_the_declared_precision_and_scale_arrive(self, attached_decimal: Any) -> None:
        types = dict(
            attached_decimal.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = 'model'"
            ).fetchall()
        )
        assert types["Total Revenue"] == "DECIMAL(18,2)"

    def test_the_values_can_actually_be_read(self, attached_decimal: Any) -> None:
        """The assertion the bug report was made of: browsing worked, reading did not."""
        from decimal import Decimal

        rows = attached_decimal.execute(
            'SELECT "Customer Country", "Total Revenue" FROM obsl.sales.model ORDER BY 1'
        ).fetchall()
        assert rows == [("UK", Decimal("75.00")), ("US", Decimal("150.00"))]
