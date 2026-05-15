"""Tests for the OBFlightServer."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.flight as flight
import pytest

from ob_flight.server import OBFlightServer


def _make_server(mgr=None, dialect: str = "duckdb") -> OBFlightServer:
    """Build a test OBFlightServer without binding the gRPC port.

    Tests construct via ``__new__`` to skip ``__init__`` (which binds
    port 8815). This helper stamps the per-instance state that v2.4.0+
    methods rely on: ``_lock`` (for the pending registry), the pending
    + prepared dicts, the TTL, batch size, dialect, and session manager.
    """
    server = OBFlightServer.__new__(OBFlightServer)
    server._session_manager = mgr
    server._default_dialect = dialect
    server._lock = threading.Lock()
    server._pending = {}
    server._prepared = {}
    server._pending_ttl = 300
    server._batch_size = 1024
    server._cache = None
    server._cache_config = None
    return server


@pytest.fixture
def mock_session_manager():
    """Mock session manager with one admin-loaded model.

    Configured for the v2.4.0+ multi-model auto-resolve path:
    ``list_protected_session_ids`` returns one id, ``get_store`` returns
    a store holding one model with empty dim/measure/metric maps and
    ``settings = None`` (so per-model dialect resolution falls back to
    the server-wide default).
    """
    model = MagicMock()
    model.data_objects = {}
    model.dimensions = {}
    model.measures = {}
    model.metrics = {}
    model.settings = None  # falls back to server _default_dialect

    model_info = MagicMock()
    model_info.model_id = "test-model"

    store = MagicMock()
    store.list_models.return_value = [model_info]
    store.get_model.return_value = model

    mgr = MagicMock()
    mgr.get_store.return_value = store
    mgr.list_protected_session_ids.return_value = ["__default__"]
    return mgr


class TestGetModel:
    def test_no_session_manager(self):
        server = _make_server(None, "duckdb")
        with pytest.raises(flight.FlightUnavailableError, match="session manager"):
            server._get_model()

    def test_no_models_loaded(self):
        """Default session exists but holds zero models → NO_MODEL_AVAILABLE."""
        mgr = MagicMock()
        store = MagicMock()
        store.list_models.return_value = []
        mgr.get_store.return_value = store
        # No protected sessions either — auto-resolve has nothing to find.
        mgr.list_protected_session_ids.return_value = []

        server = _make_server(mgr, "duckdb")
        with pytest.raises(flight.FlightUnavailableError, match="No models"):
            server._get_model()

    def test_no_default_session(self):
        """No `__default__` session, no protected sessions → NO_MODEL_AVAILABLE."""
        mgr = MagicMock()
        mgr.get_store.side_effect = KeyError("session not found")
        mgr.list_protected_session_ids.return_value = []

        server = _make_server(mgr, "duckdb")
        with pytest.raises(flight.FlightUnavailableError, match="No models"):
            server._get_model()

    def test_success(self, mock_session_manager):
        server = _make_server(mock_session_manager, "postgres")

        model, dialect = server._get_model()
        assert model is not None
        assert dialect == "postgres"

    def test_returns_first_model(self, mock_session_manager):
        server = _make_server(mock_session_manager, "duckdb")

        model, dialect = server._get_model()
        mock_session_manager.get_store.assert_called_once_with("__default__")
        assert dialect == "duckdb"


class TestCompileObml:
    def test_compile_calls_pipeline(self, mock_session_manager):
        server = _make_server(mock_session_manager, "duckdb")

        mock_pipeline_cls = MagicMock()
        mock_result = MagicMock()
        mock_result.sql = "SELECT region FROM orders"
        mock_pipeline_cls.return_value.compile.return_value = mock_result

        mock_qo_cls = MagicMock()
        mock_qo_cls.model_validate.return_value = MagicMock()

        model, _ = server._get_model()

        with patch("orionbelt.compiler.pipeline.CompilationPipeline", mock_pipeline_cls):
            with patch("orionbelt.models.query.QueryObject", mock_qo_cls):
                result = server._compile_obml(
                    {"select": {"dimensions": ["Region"]}}, model, "duckdb"
                )
                assert result.sql == "SELECT region FROM orders"
                mock_qo_cls.model_validate.assert_called_once()
                mock_pipeline_cls.return_value.compile.assert_called_once()


class TestGetFlightInfo:
    def _mock_probe(self):
        """Return a schema patch for _probe_schema."""
        return pa.schema([pa.field("n", pa.int64())])

    def test_scalar_probe_sql(self, mock_session_manager):
        """`SELECT 1` is a scalar connectivity probe — routed to catalog.

        Pre-v2.4 this hit the SQL path; v2.4+ classification treats canned
        scalar probes as catalog so they never reach the warehouse.
        """
        server = _make_server(mock_session_manager, "duckdb")

        descriptor = flight.FlightDescriptor.for_command(b"SELECT 1")
        context = MagicMock()

        with patch.object(server, "_probe_schema", return_value=self._mock_probe()):
            info = server.get_flight_info(context, descriptor)
        assert len(info.endpoints) == 1
        assert len(server._pending) == 1
        ticket_id = list(server._pending.keys())[0]
        pending = server._pending[ticket_id][0]
        # Scalar probes precompute the catalog table at get_flight_info
        # time so FlightInfo advertises the real schema (not a placeholder).
        assert pending[0] == "obsql_catalog_table"
        # Second element is the precomputed pa.Table — has rows.
        assert pending[1].num_rows >= 1

    def test_obml_query(self, mock_session_manager):
        server = _make_server(mock_session_manager, "duckdb")

        obml = b"select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
        descriptor = flight.FlightDescriptor.for_command(obml)
        context = MagicMock()

        compiled_sql = "SELECT region, SUM(amount) FROM orders GROUP BY region"
        compiled = MagicMock()
        compiled.sql = compiled_sql
        compiled.physical_tables = []
        with patch.object(server, "_compile_obml", return_value=compiled):
            with patch.object(server, "_probe_schema", return_value=self._mock_probe()):
                server.get_flight_info(context, descriptor)
        assert len(server._pending) == 1
        ticket_id = list(server._pending.keys())[0]
        pending = server._pending[ticket_id][0]
        assert pending[0] == "sql"
        assert pending[1] == compiled_sql

    def test_returns_endpoint_with_ticket(self, mock_session_manager):
        server = _make_server(mock_session_manager, "duckdb")

        descriptor = flight.FlightDescriptor.for_command(b"SELECT 42")
        context = MagicMock()

        with patch.object(server, "_probe_schema", return_value=self._mock_probe()):
            info = server.get_flight_info(context, descriptor)
        assert len(info.endpoints) == 1
        endpoint = info.endpoints[0]
        ticket_id = endpoint.ticket.ticket.decode("utf-8")
        assert ticket_id in server._pending

    def test_schema_from_scalar_probe(self, mock_session_manager):
        """Catalog scalar probes (SELECT 1) build a schema without DB I/O.

        Pre-v2.4 raw SQL like ``SELECT id, name FROM t`` was probed via
        ``_probe_schema``; v2.4+ rejects raw SQL, so this test verifies
        the remaining schema-without-DB path: the catalog scalar probe.
        """
        server = _make_server(mock_session_manager, "duckdb")
        descriptor = flight.FlightDescriptor.for_command(b"SELECT 1")
        info = server.get_flight_info(MagicMock(), descriptor)
        # Catalog scalar probe yields a non-empty schema with no warehouse hop.
        assert len(info.schema) >= 1

    def test_no_session_manager_raises(self):
        server = _make_server(None, "duckdb")

        descriptor = flight.FlightDescriptor.for_command(b"SELECT 1")
        context = MagicMock()

        with pytest.raises(flight.FlightUnavailableError):
            server.get_flight_info(context, descriptor)

    def test_flight_sql_catalog_command(self, mock_session_manager):
        """Flight SQL protobuf commands should be recognized and handled."""
        server = _make_server(mock_session_manager, "duckdb")

        # Build a minimal protobuf Any for CommandGetTables
        from ob_flight.flight_sql import CMD_GET_TABLES

        type_url_bytes = CMD_GET_TABLES.encode("utf-8")
        # protobuf: field 1 (tag 0x0a) + length + type_url
        cmd = b"\x0a" + bytes([len(type_url_bytes)]) + type_url_bytes

        descriptor = flight.FlightDescriptor.for_command(cmd)
        context = MagicMock()

        info = server.get_flight_info(context, descriptor)
        assert len(info.endpoints) == 1
        ticket_id = list(server._pending.keys())[0]
        pending = server._pending[ticket_id][0]
        assert pending[0] == "catalog"
        assert pending[1] == CMD_GET_TABLES


class TestDoGet:
    def test_unknown_ticket(self):
        server = _make_server()

        ticket = flight.Ticket(b"nonexistent")
        with pytest.raises(flight.FlightServerError, match="Unknown ticket"):
            server.do_get(MagicMock(), ticket)

    def test_execute_and_stream(self):
        server = _make_server()

        # Set up pending query (new tuple format with "sql" prefix)
        ticket_id = "test-ticket"
        server._store_pending(ticket_id, ("sql", "SELECT 1 AS n", "duckdb"))

        # Mock the DB connection
        from ob_driver_core.type_codes import NUMBER

        mock_cursor = MagicMock()
        mock_cursor.description = (("n", NUMBER, None, None, None, None, None),)
        mock_cursor.fetchmany.side_effect = [[(42.0,)], []]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("ob_flight.server.db_connect", return_value=mock_conn):
            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            stream = server.do_get(MagicMock(), ticket)
            assert stream is not None

        # Ticket should be consumed
        assert ticket_id not in server._pending
        mock_conn.close.assert_called_once()

    def test_ddl_query_returns_ok(self):
        server = _make_server()

        ticket_id = "ddl-ticket"
        server._store_pending(ticket_id, ("sql", "CREATE TABLE t (x INT)", "duckdb"))

        mock_cursor = MagicMock()
        mock_cursor.description = None  # DDL has no description

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("ob_flight.server.db_connect", return_value=mock_conn):
            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            stream = server.do_get(MagicMock(), ticket)
            assert stream is not None

        mock_conn.close.assert_called_once()

    def test_empty_result_set(self):
        server = _make_server()

        ticket_id = "empty-ticket"
        server._store_pending(ticket_id, ("sql", "SELECT * FROM t WHERE 1=0", "duckdb"))

        from ob_driver_core.type_codes import STRING

        mock_cursor = MagicMock()
        mock_cursor.description = (("name", STRING, None, None, None, None, None),)
        mock_cursor.fetchmany.return_value = []  # no rows

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("ob_flight.server.db_connect", return_value=mock_conn):
            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            stream = server.do_get(MagicMock(), ticket)
            assert stream is not None

        mock_conn.close.assert_called_once()

    def test_connection_closed_on_error(self):
        server = _make_server()

        ticket_id = "error-ticket"
        server._store_pending(ticket_id, ("sql", "SELECT bad", "duckdb"))

        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("SQL error")

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("ob_flight.server.db_connect", return_value=mock_conn):
            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            with pytest.raises(RuntimeError, match="SQL error"):
                server.do_get(MagicMock(), ticket)

        mock_conn.close.assert_called_once()

    def test_catalog_command_returns_table(self, mock_session_manager):
        """Catalog commands should return Arrow tables without DB execution."""
        server = _make_server(mock_session_manager, "duckdb")

        from ob_flight.flight_sql import CMD_GET_TABLE_TYPES

        ticket_id = "catalog-ticket"
        server._store_pending(ticket_id, ("catalog", CMD_GET_TABLE_TYPES))

        ticket = flight.Ticket(ticket_id.encode("utf-8"))
        stream = server.do_get(MagicMock(), ticket)
        assert stream is not None
        assert ticket_id not in server._pending


class TestListFlights:
    def _make_model(self) -> MagicMock:
        """Build a model with dims+measures+metrics and 2 hidden data objects."""
        dim = MagicMock()
        dim.label = "Region"
        dim.result_type = MagicMock(value="string")

        meas = MagicMock()
        meas.label = "Total Sales"
        meas.result_type = MagicMock(value="float")

        met = MagicMock()
        met.label = "Avg Sales"

        obj1 = MagicMock()
        obj1.columns = {"ID": MagicMock(label="ID", abstract_type="int")}
        obj2 = MagicMock()
        obj2.columns = {"Name": MagicMock(label="Name", abstract_type="string")}

        model = MagicMock()
        model.label = "sales"
        model.data_objects = {"Orders": obj1, "Customers": obj2}
        model.dimensions = {"Region": dim}
        model.measures = {"Total Sales": meas}
        model.metrics = {"Avg Sales": met}
        return model

    def test_hides_data_objects_in_default_mode(self, mock_session_manager):
        """v2.4.0+ hides data-object physical tables from the BI tool tree.

        The advertised entries are: 1 semantic virtual table + 3 label
        views (``_dimensions`` / ``_measures`` / ``_metrics``) + 3
        metadata views (``_..._metadata``) = 7. Data objects are NOT
        exposed.
        """
        model = self._make_model()
        mock_session_manager.get_store.return_value.get_model.return_value = model
        server = _make_server(mock_session_manager, "duckdb")

        infos = list(server.list_flights(MagicMock(), b""))
        assert len(infos) == 7
        # No data-object label appears in the descriptor paths.
        leaves = {info.descriptor.path[-1] for info in infos}
        assert b"Orders" not in leaves
        assert b"Customers" not in leaves

    def test_label_views_split_per_category(self, mock_session_manager):
        """Label views should each list only their category's labels."""
        model = self._make_model()
        mock_session_manager.get_store.return_value.get_model.return_value = model
        server = _make_server(mock_session_manager, "duckdb")

        infos = list(server.list_flights(MagicMock(), b""))
        by_name = {info.descriptor.path[-1].decode(): info for info in infos}

        # Label views — one field per dim/measure/metric label.
        assert {f.name for f in by_name["dimensions"].schema} == {"Region"}
        assert {f.name for f in by_name["measures"].schema} == {"Total Sales"}
        assert {f.name for f in by_name["metrics"].schema} == {"Avg Sales"}

        # Metadata views — fixed introspection columns.
        meta_dim_cols = {f.name for f in by_name["_dimensions_metadata"].schema}
        assert {"name", "data_object", "column", "type"} <= meta_dim_cols

    def test_no_model_returns_empty(self):
        server = _make_server(None, "duckdb")

        infos = list(server.list_flights(MagicMock(), b""))
        assert len(infos) == 0


class TestVirtualTables:
    def test_detect_dimensions(self):
        assert (
            OBFlightServer._detect_virtual_table("SELECT * FROM _dimensions_metadata")
            == "_dimensions_metadata"
        )

    def test_detect_measures_quoted(self):
        sql = 'SELECT * FROM "orionbelt"."model"."_measures_metadata" LIMIT 200'
        assert OBFlightServer._detect_virtual_table(sql) == "_measures_metadata"

    def test_detect_metrics(self):
        assert (
            OBFlightServer._detect_virtual_table("SELECT * FROM _metrics_metadata")
            == "_metrics_metadata"
        )

    def test_no_virtual_table(self):
        assert OBFlightServer._detect_virtual_table("SELECT * FROM orders") is None

    def test_probe_schema_returns_virtual_schema(self, mock_session_manager):
        server = _make_server(mock_session_manager, "duckdb")

        schema = server._probe_schema("SELECT * FROM _dimensions_metadata", "duckdb")
        assert schema.field(0).name == "name"
        assert schema.field(1).name == "data_object"

    def test_query_dimensions(self, mock_session_manager):
        dim = MagicMock()
        dim.label = "Region"
        dim.view = "Orders"
        dim.column = "region"
        dim.result_type = MagicMock(value="string")
        dim.time_grain = None
        dim.description = None

        model = MagicMock()
        model.data_objects = {}
        model.dimensions = {"Region": dim}

        mock_session_manager.get_store.return_value.get_model.return_value = model

        server = _make_server(mock_session_manager, "duckdb")

        stream = server._query_virtual_table("_dimensions_metadata")
        assert stream is not None

    def test_execute_sql_intercepts_virtual_table(self, mock_session_manager):
        dim = MagicMock()
        dim.label = "Region"
        dim.view = "Orders"
        dim.column = "region"
        dim.result_type = MagicMock(value="string")
        dim.time_grain = None
        dim.description = "test"

        model = MagicMock()
        model.data_objects = {}
        model.dimensions = {"Region": dim}

        mock_session_manager.get_store.return_value.get_model.return_value = model

        server = _make_server(mock_session_manager, "duckdb")

        # Should NOT hit the database — returns virtual table data
        stream = server._execute_sql('SELECT * FROM "_dimensions_metadata"', "duckdb")
        assert stream is not None


class TestFlightCacheEnvelope:
    """Regression: Flight cache writes must use the shared parquet_codec
    envelope so REST readers can decode ``sql``/``dialect``/``columns``
    instead of falling back to empty defaults.
    """

    def test_flight_cache_payload_is_decodable_by_rest(self) -> None:
        from orionbelt.cache import parquet_codec

        server = _make_server()
        captured: dict = {}

        class FakeCache:
            async def set(self, key, payload, **kwargs):
                captured["payload"] = payload
                captured["kwargs"] = kwargs
                captured["key"] = key

        server._cache = FakeCache()

        table = pa.table({"id": [1, 2], "name": ["alice", "bob"]})
        server._cache_put_table(
            table,
            {
                "key": "k1",
                "ttl": 60,
                "physical_tables": ["main.t"],
                "session_id": "s",
                "model_id": "m",
                "sql": "SELECT id, name FROM t",
                "dialect": "postgres",
            },
        )

        env = parquet_codec.decode(captured["payload"])
        assert env.sql == "SELECT id, name FROM t"
        assert env.dialect == "postgres"
        assert env.physical_tables == ["main.t"]
        assert [c["name"] for c in env.columns] == ["id", "name"]
        assert env.rows == [[1, "alice"], [2, "bob"]]
