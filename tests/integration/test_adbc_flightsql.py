"""ADBC conformance harness for the OBSL Flight SQL surface.

Track II-1 of ``design/PLAN_adbc.md``. OBSL implements the Flight SQL
command set, and ADBC's ``flightsql`` driver speaks exactly that — so
"any ADBC application can reach the semantic layer" *should* be true.
Nothing verified it. This harness does, by driving a real
``OBFlightServer`` on an ephemeral port through the real driver.

Deliberately end-to-end: the fixture seeds a genuine DuckDB file and
points ``DUCKDB_DATABASE`` at it, so queries compile *and execute*. A
mocked executor would prove the protocol works while hiding every type
and value question, which is most of what we need answered.

Run with::

    uv run pytest -m adbc_flight

Distinct from the ``adbc`` marker, which means "needs a live PostgreSQL"
(see ``test_adbc_postgres.py``) and belongs to the other ADBC track:
OBSL as a *client*. This file is OBSL as a *server*.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from tests.conftest import SAMPLE_MODEL_YAML

pytestmark = pytest.mark.adbc_flight

MODEL_NAME = "sales"

# Mirrors tests/integration/test_duckdb_execution.py's sample setup: the
# physical tables SAMPLE_MODEL_YAML's dataObjects point at.
_SETUP_SQL = """
CREATE SCHEMA IF NOT EXISTS PUBLIC;
CREATE OR REPLACE TABLE PUBLIC.CUSTOMERS (CUSTOMER_ID VARCHAR, COUNTRY VARCHAR);
INSERT INTO PUBLIC.CUSTOMERS VALUES ('C1', 'US'), ('C2', 'UK');
CREATE OR REPLACE TABLE PUBLIC.ORDERS (ORDER_ID VARCHAR, CUSTOMER_ID VARCHAR, AMOUNT DOUBLE);
INSERT INTO PUBLIC.ORDERS VALUES ('O1', 'C1', 100.0), ('O2', 'C1', 50.0), ('O3', 'C2', 75.0);
"""


@pytest.fixture(scope="module")
def flight_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A live OBFlightServer over a real DuckDB file. Yields its grpc URI."""
    pytest.importorskip("adbc_driver_flightsql", reason="ADBC Flight SQL driver not installed")
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("ob_flight", reason="ob-flight-extension not installed")

    from ob_flight.server import OBFlightServer

    from orionbelt.service.session_manager import SessionManager

    db_path = tmp_path_factory.mktemp("adbc") / "sample.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_SETUP_SQL)
    conn.close()

    prev = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(db_path)

    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    mgr.get_or_create_named(MODEL_NAME).load_model(SAMPLE_MODEL_YAML, dedup=False)

    # Port 0 lets the OS pick a free port, so parallel runs cannot collide.
    server = OBFlightServer("grpc://127.0.0.1:0", session_manager=mgr, default_dialect="duckdb")
    thread = threading.Thread(target=server.serve, name="adbc-harness-flight", daemon=True)
    thread.start()
    try:
        yield f"grpc://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        if prev is None:
            os.environ.pop("DUCKDB_DATABASE", None)
        else:
            os.environ["DUCKDB_DATABASE"] = prev


@pytest.fixture
def conn(flight_uri: str) -> Iterator[Any]:
    """An ADBC DB-API connection to the harness server."""
    from adbc_driver_flightsql import dbapi

    connection = dbapi.connect(flight_uri)
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Connect and negotiate
# ---------------------------------------------------------------------------


class TestConnect:
    def test_handshake(self, conn: Any) -> None:
        """The bare connect must work — everything else is downstream."""
        assert conn is not None

    def test_get_info(self, conn: Any) -> None:
        """ADBC drivers branch on SqlInfo, so it has to answer."""
        info = conn.adbc_get_info()
        assert isinstance(info, dict)
        assert info

    def test_get_table_types(self, conn: Any) -> None:
        """BI tools call this before anything else."""
        types = conn.adbc_get_table_types()
        assert isinstance(types, list)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_get_objects_lists_the_model(self, conn: Any) -> None:
        """ADBC's depth semantics differ from Flight SQL's filter semantics.

        This is the likeliest mismatch in the whole surface: OBSL answers
        ``CommandGetTables`` with a ``table_name_filter_pattern``, while
        ADBC asks for a depth and expects the nesting to come back whole.
        """
        objects = conn.adbc_get_objects(depth="all").read_all().to_pylist()
        assert objects, "no catalogs returned"
        schemas = [s for cat in objects for s in (cat.get("catalog_db_schemas") or [])]
        assert schemas, f"no db_schemas under catalogs: {objects}"
        names = {s.get("db_schema_name") for s in schemas}
        assert MODEL_NAME in names, f"model schema missing, saw {names}"

    def test_get_objects_exposes_the_virtual_table(self, conn: Any) -> None:
        objects = conn.adbc_get_objects(depth="all").read_all().to_pylist()
        tables = [
            t
            for cat in objects
            for s in (cat.get("catalog_db_schemas") or [])
            for t in (s.get("db_schema_tables") or [])
        ]
        assert tables, "no tables returned at depth=all"
        assert "model" in {t.get("table_name") for t in tables}

    def test_get_objects_columns_are_artefacts(self, conn: Any) -> None:
        """The model relation's columns are the model's artefacts."""
        objects = conn.adbc_get_objects(depth="all").read_all().to_pylist()
        cols: list[str] = [
            c.get("column_name", "")
            for cat in objects
            for s in (cat.get("catalog_db_schemas") or [])
            for t in (s.get("db_schema_tables") or [])
            if t.get("table_name") == "model"
            for c in (t.get("table_columns") or [])
        ]
        assert "Customer Country" in cols, cols
        assert "Total Revenue" in cols, cols

    def test_get_table_schema(self, conn: Any) -> None:
        schema = conn.adbc_get_table_schema("model", db_schema_filter=MODEL_NAME)
        assert "Customer Country" in schema.names
        assert "Total Revenue" in schema.names


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _fetch(conn: Any, sql: str) -> Any:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return cur.fetch_arrow_table()
    finally:
        cur.close()


class TestExecution:
    def test_obsql_returns_rows(self, conn: Any) -> None:
        """The whole point: a semantic query, executed, as Arrow."""
        table = _fetch(conn, f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}')
        assert table.num_rows == 2
        by_country = {r["Customer Country"]: r["Total Revenue"] for r in table.to_pylist()}
        assert by_country["US"] == pytest.approx(150.0)
        assert by_country["UK"] == pytest.approx(75.0)

    def test_where_is_pushed_into_the_semantic_query(self, conn: Any) -> None:
        table = _fetch(
            conn,
            f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME} '
            "WHERE \"Customer Country\" = 'US'",
        )
        assert table.to_pylist() == [{"Customer Country": "US", "Total Revenue": 150.0}]

    def test_qualified_from_target(self, conn: Any) -> None:
        """A qualified name must resolve to the model, not be rejected.

        ADBC clients and BI tools qualify table names from the catalog
        they just browsed. pgwire needed ``_normalize_for_obsql`` /
        ``_resolve_model_alias`` for exactly this; the Flight
        ``classify_sql`` path has no equivalent, so an unrecognised FROM
        target falls to ``_MODE_REJECTED`` -> ``RAW_SQL_REJECTED``.
        """
        table = _fetch(
            conn,
            f'SELECT "Customer Country", "Total Revenue" FROM "{MODEL_NAME}"."model"',
        )
        assert table.num_rows == 2

    def test_qualified_star_still_previews_metadata(self, conn: Any) -> None:
        """``SELECT *`` over a qualified model stays a metadata preview.

        Guards the narrowing that fixed ``test_qualified_from_target``:
        BI tools fire this after picking the table out of the schema tree
        and expect introspection rows, so only artefact projections were
        rerouted to the semantic path.
        """
        table = _fetch(conn, f'SELECT * FROM "{MODEL_NAME}"."model"')
        assert "column_name" in table.column_names, table.column_names

    def test_empty_result_keeps_its_schema(self, conn: Any) -> None:
        """A zero-row result must still describe its columns."""
        table = _fetch(
            conn,
            f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME} '
            "WHERE \"Customer Country\" = '__none__'",
        )
        assert table.num_rows == 0
        assert table.column_names == ["Customer Country", "Total Revenue"]
        assert not any(str(f.type) == "null" for f in table.schema), [
            (f.name, str(f.type)) for f in table.schema
        ]

    def test_rejected_query_raises(self, conn: Any) -> None:
        """A refusal must arrive as an exception, not an empty result."""
        with pytest.raises(Exception, match="(?i)reject|unsupported|not.*support|error"):
            _fetch(conn, "SELECT * FROM nonexistent_relation_xyz")

    def test_select_star_is_refused(self, conn: Any) -> None:
        """``SELECT *`` over the model relation is a governance refusal."""
        with pytest.raises(Exception, match="(?i)reject|unsupported|\\*"):
            _fetch(conn, f"SELECT * FROM {MODEL_NAME}")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Parameter binding is unimplemented. The placeholder reaches "
            "translate_sql_to_query at Prepare time, before any value is "
            "bound, and the OBSQL translator has no notion of one: "
            'UNSUPPORTED_SQL_FEATURE \'Unsupported predicate "x" = ?. Only '
            "`column op literal` shapes are accepted.' Supporting it needs "
            "placeholders in the translator plus DoPut binding. Tracked as "
            "Track II-2 in design/PLAN_adbc.md."
        ),
    )
    def test_prepared_statement_with_parameters(self, conn: Any) -> None:
        """ADBC clients bind parameters; OBSL must accept or refuse clearly.

        ``CommandPreparedStatementQuery`` is implemented, but binding
        goes through ``DoPut`` and is not.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME} '
                'WHERE "Customer Country" = ?',
                parameters=("US",),
            )
            table = cur.fetch_arrow_table()
        finally:
            cur.close()
        assert table.num_rows == 1


# ---------------------------------------------------------------------------
# Type fidelity — feeds the II-6 matrix
# ---------------------------------------------------------------------------


class TestTypeFidelity:
    def test_measure_is_numeric_not_text(self, conn: Any) -> None:
        """A measure arriving as string is the bug that broke Tableau."""
        import pyarrow as pa

        table = _fetch(conn, f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}')
        rev = table.schema.field("Total Revenue").type
        assert pa.types.is_floating(rev) or pa.types.is_decimal(rev) or pa.types.is_integer(rev), (
            f"Total Revenue arrived as {rev}"
        )

    def test_dimension_is_string(self, conn: Any) -> None:
        import pyarrow as pa

        table = _fetch(conn, f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}')
        assert pa.types.is_string(table.schema.field("Customer Country").type)

    def test_count_measure_is_integer(self, conn: Any) -> None:
        import pyarrow as pa

        table = _fetch(conn, f'SELECT "Customer Country", "Order Count" FROM {MODEL_NAME}')
        assert pa.types.is_integer(table.schema.field("Order Count").type)


# ---------------------------------------------------------------------------
# Multi-model addressing
# ---------------------------------------------------------------------------


class TestModelSelection:
    def test_model_selected_by_call_header(self, flight_uri: str) -> None:
        """The documented way to pick a model over ADBC.

        Flight routing reads the model from the gRPC ``database`` /
        ``x-obsl-model`` header; ADBC sets arbitrary headers through
        ``adbc.flight.sql.rpc.call_header.*``.
        """
        from adbc_driver_flightsql import dbapi

        connection = dbapi.connect(
            flight_uri,
            db_kwargs={"adbc.flight.sql.rpc.call_header.x-obsl-model": MODEL_NAME},
        )
        try:
            table = _fetch(
                connection, f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}'
            )
            assert table.num_rows == 2
        finally:
            connection.close()
