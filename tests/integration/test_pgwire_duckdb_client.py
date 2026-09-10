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
import os
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.session_manager import SessionManager
from tests.conftest import SAMPLE_MODEL_YAML
from tests.integration.test_adbc_flightsql import _SETUP_SQL

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

    def test_count_star(self, attached: Any) -> None:
        """DuckDB sends this as ``SELECT NULL FROM model`` and counts the rows.

        The answer is the model's own grain - one row per dimension
        combination - which is what ``SELECT *`` over it returns.
        """
        assert attached.execute("SELECT count(*) FROM obsl.sales.model").fetchone() == (2,)


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
