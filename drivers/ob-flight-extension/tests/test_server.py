"""Tests for the OBFlightServer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.flight as flight
import pytest

from ob_flight.server import OBFlightServer


@pytest.fixture
def mock_session_manager():
    """Mock session manager with a default session and model."""
    model = MagicMock()
    model.data_objects = {}

    model_info = MagicMock()
    model_info.model_id = "test-model"

    store = MagicMock()
    store.list_models.return_value = [model_info]
    store.get_model.return_value = model

    mgr = MagicMock()
    mgr.get_store.return_value = store
    return mgr


class TestGetModel:
    def test_no_session_manager(self):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = None
        server._default_dialect = "duckdb"
        with pytest.raises(flight.FlightUnavailableError, match="session manager"):
            server._get_model()

    def test_no_models_loaded(self):
        mgr = MagicMock()
        store = MagicMock()
        store.list_models.return_value = []
        mgr.get_store.return_value = store

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mgr
        server._default_dialect = "duckdb"
        with pytest.raises(flight.FlightUnavailableError, match="No models"):
            server._get_model()

    def test_no_default_session(self):
        mgr = MagicMock()
        mgr.get_store.side_effect = KeyError("session not found")

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mgr
        server._default_dialect = "duckdb"
        with pytest.raises(flight.FlightUnavailableError, match="default session"):
            server._get_model()

    def test_success(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "postgres"

        model, dialect = server._get_model()
        assert model is not None
        assert dialect == "postgres"

    def test_returns_first_model(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"

        model, dialect = server._get_model()
        mock_session_manager.get_store.assert_called_once_with("__default__")
        assert dialect == "duckdb"


class TestCompileObml:
    def test_compile_calls_pipeline(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"

        mock_pipeline_cls = MagicMock()
        mock_result = MagicMock()
        mock_result.sql = "SELECT region FROM orders"
        mock_pipeline_cls.return_value.compile.return_value = mock_result

        mock_qo_cls = MagicMock()
        mock_qo_cls.model_validate.return_value = MagicMock()

        model, _ = server._get_model()

        with patch(
            "orionbelt.compiler.pipeline.CompilationPipeline", mock_pipeline_cls
        ):
            with patch("orionbelt.models.query.QueryObject", mock_qo_cls):
                sql = server._compile_obml(
                    {"select": {"dimensions": ["Region"]}}, model, "duckdb"
                )
                assert sql == "SELECT region FROM orders"
                mock_qo_cls.model_validate.assert_called_once()
                mock_pipeline_cls.return_value.compile.assert_called_once()


class TestGetFlightInfo:
    def _mock_probe(self):
        """Return a schema patch for _probe_schema."""
        return pa.schema([pa.field("n", pa.int64())])

    def test_plain_sql(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._pending = {}

        descriptor = flight.FlightDescriptor.for_command(b"SELECT 1")
        context = MagicMock()

        with patch.object(server, "_probe_schema", return_value=self._mock_probe()):
            info = server.get_flight_info(context, descriptor)
        assert len(info.endpoints) == 1
        # Ticket should be stored
        assert len(server._pending) == 1
        ticket_id = list(server._pending.keys())[0]
        pending = server._pending[ticket_id]
        assert pending[0] == "sql"
        assert pending[1] == "SELECT 1"
        assert pending[2] == "duckdb"

    def test_obml_query(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._pending = {}

        obml = b"select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
        descriptor = flight.FlightDescriptor.for_command(obml)
        context = MagicMock()

        compiled_sql = "SELECT region, SUM(amount) FROM orders GROUP BY region"
        with patch.object(server, "_compile_obml", return_value=compiled_sql):
            with patch.object(server, "_probe_schema", return_value=self._mock_probe()):
                server.get_flight_info(context, descriptor)
        assert len(server._pending) == 1
        ticket_id = list(server._pending.keys())[0]
        pending = server._pending[ticket_id]
        assert pending[0] == "sql"
        assert pending[1] == compiled_sql

    def test_returns_endpoint_with_ticket(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._pending = {}

        descriptor = flight.FlightDescriptor.for_command(b"SELECT 42")
        context = MagicMock()

        with patch.object(server, "_probe_schema", return_value=self._mock_probe()):
            info = server.get_flight_info(context, descriptor)
        assert len(info.endpoints) == 1
        endpoint = info.endpoints[0]
        ticket_id = endpoint.ticket.ticket.decode("utf-8")
        assert ticket_id in server._pending

    def test_schema_from_probe(self, mock_session_manager):
        """FlightInfo schema should come from _probe_schema, not a placeholder."""
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._pending = {}

        real_schema = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.utf8())])
        descriptor = flight.FlightDescriptor.for_command(b"SELECT id, name FROM t")
        context = MagicMock()

        with patch.object(server, "_probe_schema", return_value=real_schema):
            info = server.get_flight_info(context, descriptor)
        assert info.schema == real_schema

    def test_no_session_manager_raises(self):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = None
        server._default_dialect = "duckdb"
        server._pending = {}

        descriptor = flight.FlightDescriptor.for_command(b"SELECT 1")
        context = MagicMock()

        with pytest.raises(flight.FlightUnavailableError):
            server.get_flight_info(context, descriptor)

    def test_flight_sql_catalog_command(self, mock_session_manager):
        """Flight SQL protobuf commands should be recognized and handled."""
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._pending = {}

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
        pending = server._pending[ticket_id]
        assert pending[0] == "catalog"
        assert pending[1] == CMD_GET_TABLES


class TestDoGet:
    def test_unknown_ticket(self):
        server = OBFlightServer.__new__(OBFlightServer)
        server._pending = {}

        ticket = flight.Ticket(b"nonexistent")
        with pytest.raises(flight.FlightServerError, match="Unknown ticket"):
            server.do_get(MagicMock(), ticket)

    def test_execute_and_stream(self):
        server = OBFlightServer.__new__(OBFlightServer)
        server._batch_size = 1024

        # Set up pending query (new tuple format with "sql" prefix)
        ticket_id = "test-ticket"
        server._pending = {ticket_id: ("sql", "SELECT 1 AS n", "duckdb")}

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
        server = OBFlightServer.__new__(OBFlightServer)
        server._batch_size = 1024

        ticket_id = "ddl-ticket"
        server._pending = {ticket_id: ("sql", "CREATE TABLE t (x INT)", "duckdb")}

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
        server = OBFlightServer.__new__(OBFlightServer)
        server._batch_size = 1024

        ticket_id = "empty-ticket"
        server._pending = {
            ticket_id: ("sql", "SELECT * FROM t WHERE 1=0", "duckdb")
        }

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
        server = OBFlightServer.__new__(OBFlightServer)
        server._batch_size = 1024

        ticket_id = "error-ticket"
        server._pending = {ticket_id: ("sql", "SELECT bad", "duckdb")}

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
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._batch_size = 1024

        from ob_flight.flight_sql import CMD_GET_TABLE_TYPES

        ticket_id = "catalog-ticket"
        server._pending = {ticket_id: ("catalog", CMD_GET_TABLE_TYPES)}

        ticket = flight.Ticket(ticket_id.encode("utf-8"))
        stream = server.do_get(MagicMock(), ticket)
        assert stream is not None
        assert ticket_id not in server._pending


class TestListFlights:
    def test_lists_model_data_objects(self, mock_session_manager):
        """list_flights should show semantic model data objects as tables."""
        col1 = MagicMock()
        col1.label = "ID"
        col1.abstract_type = "int"
        obj1 = MagicMock()
        obj1.columns = {"ID": col1}

        col2 = MagicMock()
        col2.label = "Name"
        col2.abstract_type = "string"
        obj2 = MagicMock()
        obj2.columns = {"Name": col2}

        model = MagicMock()
        model.data_objects = {"Orders": obj1, "Customers": obj2}

        mock_session_manager.get_store.return_value.get_model.return_value = model

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"

        infos = list(server.list_flights(MagicMock(), b""))
        # 2 data objects + 3 virtual tables (_dimensions, _measures, _metrics)
        assert len(infos) == 5

    def test_shows_correct_columns_per_data_object(self, mock_session_manager):
        """Each data object should only show its own columns."""
        col_id = MagicMock()
        col_id.label = "order_id"
        col_id.abstract_type = "int"
        col_date = MagicMock()
        col_date.label = "order_date"
        col_date.abstract_type = "date"
        obj_orders = MagicMock()
        obj_orders.columns = {"order_id": col_id, "order_date": col_date}

        col_name = MagicMock()
        col_name.label = "name"
        col_name.abstract_type = "string"
        obj_customers = MagicMock()
        obj_customers.columns = {"name": col_name}

        model = MagicMock()
        model.data_objects = {"Orders": obj_orders, "Customers": obj_customers}

        mock_session_manager.get_store.return_value.get_model.return_value = model

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"

        infos = list(server.list_flights(MagicMock(), b""))
        # 2 data objects + 3 virtual tables
        assert len(infos) == 5
        # Orders should have 2 columns
        assert len(infos[0].schema) == 2
        # Customers should have 1 column
        assert len(infos[1].schema) == 1
        # Virtual tables should be present
        vt_names = {info.descriptor.path[-1] for info in infos[2:]}
        assert vt_names == {b"_dimensions", b"_measures", b"_metrics"}

    def test_no_model_returns_empty(self):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = None
        server._default_dialect = "duckdb"

        infos = list(server.list_flights(MagicMock(), b""))
        assert len(infos) == 0


class TestVirtualTables:
    def test_detect_dimensions(self):
        assert OBFlightServer._detect_virtual_table("SELECT * FROM _dimensions") == "_dimensions"

    def test_detect_measures_quoted(self):
        sql = 'SELECT * FROM "orionbelt"."model"."_measures" LIMIT 200'
        assert OBFlightServer._detect_virtual_table(sql) == "_measures"

    def test_detect_metrics(self):
        assert OBFlightServer._detect_virtual_table("SELECT * FROM _metrics") == "_metrics"

    def test_no_virtual_table(self):
        assert OBFlightServer._detect_virtual_table("SELECT * FROM orders") is None

    def test_probe_schema_returns_virtual_schema(self, mock_session_manager):
        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"

        schema = server._probe_schema("SELECT * FROM _dimensions", "duckdb")
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

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"

        stream = server._query_virtual_table("_dimensions")
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

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = mock_session_manager
        server._default_dialect = "duckdb"
        server._batch_size = 1024

        # Should NOT hit the database — returns virtual table data
        stream = server._execute_sql('SELECT * FROM "_dimensions"', "duckdb")
        assert stream is not None
