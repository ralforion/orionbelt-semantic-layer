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
    """Regression: Flight cache writes must use the shared data-only codec
    (``encode_data`` blob + ``columns_json`` schema sidecar) so REST/pgwire
    readers decode the same entry — one entry per compiled query across all
    surfaces. (The blob is data only; sql/dialect/types are metadata each
    surface rebuilds fresh, carried as ``cache.set`` kwargs.)
    """

    def test_flight_cache_payload_is_decodable_by_rest(self) -> None:
        import json

        from orionbelt.cache.result_codec import decode_data

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
                "datasource": "postgres",
                "model_id": "m",
                "sql": "SELECT id, name FROM t",
                "dialect": "postgres",
            },
        )

        # The blob is a pure Arrow data stream any surface can decode.
        table_back = decode_data(captured["payload"])
        assert table_back.column_names == ["id", "name"]
        assert table_back.to_pylist() == [
            {"id": 1, "name": "alice"},
            {"id": 2, "name": "bob"},
        ]

        # Envelope metadata rides as cache.set kwargs, rebuilt fresh per read.
        kwargs = captured["kwargs"]
        assert kwargs["dialect"] == "postgres"
        assert kwargs["physical_tables"] == ["main.t"]
        assert kwargs["model_id"] == "m"
        assert kwargs["row_count"] == 2
        # The datasource scoping reaches the backend (shared key across surfaces).
        assert kwargs["datasource"] == "postgres"
        # Column schema sidecar preserves names for a cross-surface reader.
        cols = json.loads(kwargs["columns_json"])
        assert [c["name"] for c in cols] == ["id", "name"]

    def test_flight_cache_column_type_key_matches_rest_decoder(self) -> None:
        """Regression: Flight encoded column metadata under ``data_type`` but
        REST's decoder reads ``type``. A REST hit on a Flight-written entry
        decoded every column as ``string``, dropping numeric/datetime types.
        Types now live in the ``columns_json`` sidecar under ``type``.
        """
        import json

        server = _make_server()
        captured: dict = {}

        class FakeCache:
            async def set(self, key, payload, **kwargs):
                captured["kwargs"] = kwargs

        server._cache = FakeCache()

        # Mix of numeric, datetime, and string columns to exercise the
        # Arrow-to-type-hint mapping.
        table = pa.table(
            {
                "id": pa.array([1, 2], type=pa.int64()),
                "amount": pa.array([1.5, 2.5], type=pa.float64()),
                "ts": pa.array([0, 1], type=pa.timestamp("us")),
                "name": pa.array(["a", "b"], type=pa.utf8()),
            }
        )
        server._cache_put_table(
            table,
            {
                "key": "k2",
                "ttl": 60,
                "physical_tables": [],
                "datasource": "postgres",
                "model_id": "m",
                "sql": "SELECT id, amount, ts, name FROM t",
                "dialect": "postgres",
            },
        )

        cols = json.loads(captured["kwargs"]["columns_json"])
        by_name = {c["name"]: c for c in cols}
        # REST schema uses ``type`` (the Pydantic field name), not ``data_type``.
        # Vocabulary: string / number / datetime / binary.
        assert by_name["id"].get("type") == "number"
        assert by_name["amount"].get("type") == "number"
        assert by_name["ts"].get("type") == "datetime"
        assert by_name["name"].get("type") == "string"
        # The old (broken) key must NOT be present.
        assert all("data_type" not in c for c in cols)

    @staticmethod
    def _roundtrip_server():
        """A server whose FakeCache stores set() payloads and serves them on
        get(), so a put/get pair exercises the real codec byte format."""
        from datetime import UTC, datetime

        from orionbelt.cache.protocol import CachedResult

        server = _make_server()
        store: dict = {}

        class FakeCache:
            async def set(self, key, payload, **kwargs):
                store[key] = CachedResult(
                    payload=payload,
                    cached_at=datetime.now(UTC),
                    ttl_remaining_seconds=kwargs.get("ttl_seconds", 60),
                    physical_tables=kwargs.get("physical_tables", []),
                    row_count=kwargs.get("row_count", 0),
                )

            async def get(self, key):
                return store.get(key)

        server._cache = FakeCache()
        return server

    @staticmethod
    def _meta(key: str) -> dict:
        return {
            "key": key,
            "ttl": 60,
            "physical_tables": [],
            "datasource": "duckdb",
            "model_id": "m",
            "sql": "SELECT id, name FROM t",
            "dialect": "duckdb",
        }

    def test_flight_cache_roundtrip_get_after_put(self) -> None:
        """A Flight writer's entry is read back by the Flight reader: the
        ``encode_table`` blob decodes cleanly via ``decode_data`` (the same byte
        format ``encode_data`` writes), proving get/set share one codec."""
        server = self._roundtrip_server()

        table = pa.table({"id": [7, 8, 9], "name": ["x", "y", "z"]})
        server._cache_put_table(table, self._meta("rt"))

        got = server._cache_get_table("rt")
        assert got is not None
        assert got.column_names == ["id", "name"]
        assert got.to_pylist() == [
            {"id": 7, "name": "x"},
            {"id": 8, "name": "y"},
            {"id": 9, "name": "z"},
        ]

    def test_flight_cache_preserves_schema_for_empty_result(self) -> None:
        """Regression: an empty typed result must keep its Arrow schema through
        the cache. ``encode_data`` re-infers types from values, so an empty
        ``int64``/``string`` table came back ``null``/``null`` — a cache hit
        would then stream a schema that no longer matches the one Flight
        advertised in FlightInfo. ``encode_table`` preserves the exact schema."""
        server = self._roundtrip_server()

        empty = pa.table(
            {
                "id": pa.array([], type=pa.int64()),
                "amount": pa.array([], type=pa.float64()),
                "ts": pa.array([], type=pa.timestamp("us")),
                "name": pa.array([], type=pa.utf8()),
            }
        )
        server._cache_put_table(empty, self._meta("empty"))

        got = server._cache_get_table("empty")
        assert got is not None
        assert got.num_rows == 0
        assert got.schema.field("id").type == pa.int64()
        assert got.schema.field("amount").type == pa.float64()
        assert got.schema.field("ts").type == pa.timestamp("us")
        assert got.schema.field("name").type == pa.utf8()

    def test_flight_cache_preserves_schema_for_all_null_result(self) -> None:
        """All-null typed columns must also keep their declared Arrow types."""
        server = self._roundtrip_server()

        all_null = pa.table(
            {
                "id": pa.array([None, None], type=pa.int64()),
                "name": pa.array([None, None], type=pa.utf8()),
            }
        )
        server._cache_put_table(all_null, self._meta("allnull"))

        got = server._cache_get_table("allnull")
        assert got is not None
        assert got.num_rows == 2
        assert got.schema.field("id").type == pa.int64()
        assert got.schema.field("name").type == pa.utf8()
        assert got.to_pylist() == [
            {"id": None, "name": None},
            {"id": None, "name": None},
        ]


class TestFlightBuildCacheMeta:
    """Flight's ``build_cache_meta`` delegates key + TTL derivation to the
    shared ``resolve_cache_plan`` (issue #126), so a compiled query keys and
    TTLs identically on Flight and REST/pgwire. This exercises the real
    delegation path end-to-end (server method -> server_execution ->
    resolve_cache_plan), not just the shared plan in isolation.
    """

    def _server_with_cache(self, mock_session_manager, dialect: str = "postgres"):
        server = _make_server(mock_session_manager, dialect)

        class FakeCache:
            backend_name = "memory"

            def heartbeats_snapshot(self):
                return {}

        class FakeConfig:
            min_ttl_seconds = 5
            max_ttl_seconds = 86400
            unknown_policy = "no_cache"
            unknown_default_ttl_seconds = 300

        server._cache = FakeCache()
        server._cache_config = FakeConfig()
        # compute_effective_ttl calls ``contracts.get(...)`` — hand it a real
        # dict, not the MagicMock store's auto-attr.
        mock_session_manager.get_store.return_value.refresh_contracts.return_value = {}
        return server

    def test_key_matches_rest_derivation(self, mock_session_manager):
        """The Flight-derived key equals the canonical key REST/pgwire build
        for the same compiled query — the cross-surface sharing guarantee."""
        from orionbelt.cache.key import build_cache_key, build_datasource_key

        server = self._server_with_cache(mock_session_manager)
        sql = "SELECT a FROM t GROUP BY a"
        meta = server._build_cache_meta(
            compiled_sql=sql, dialect="postgres", context=None, physical_tables=[]
        )
        assert meta is not None
        assert meta["key"] == build_cache_key(
            datasource=build_datasource_key("postgres"),
            model_id="test-model",
            dialect="postgres",
            sql=sql,
        )
        assert meta["datasource"] == "postgres"
        assert meta["dialect"] == "postgres"
        assert meta["model_id"] == "test-model"
        # Empty physical_tables -> all-static -> capped at max TTL.
        assert meta["ttl"] == 86400

    def test_nondeterministic_sql_returns_none(self, mock_session_manager):
        server = self._server_with_cache(mock_session_manager)
        meta = server._build_cache_meta(
            compiled_sql="SELECT NOW()",
            dialect="postgres",
            context=None,
            physical_tables=[],
        )
        assert meta is None

    def test_noop_backend_skips_cache(self, mock_session_manager):
        server = self._server_with_cache(mock_session_manager)
        server._cache.backend_name = "noop"
        meta = server._build_cache_meta(
            compiled_sql="SELECT a FROM t",
            dialect="postgres",
            context=None,
            physical_tables=[],
        )
        assert meta is None


class TestFlightCatalogFilter:
    """Regression: ``CommandGetTables`` / ``CommandGetColumns`` ignored
    protobuf field 1 (catalog), so BI clients selecting a catalog via the
    command body got fallback metadata instead of the chosen model's.
    """

    def test_parse_catalog_filter_extracts_field_1(self) -> None:
        from ob_flight.flight_sql import parse_catalog_filter

        # Hand-build a minimal proto body with field 1 (string) = "commerce"
        # and field 3 (string) = "_dimensions". Tag = (field << 3) | wire_type.
        # wire_type=2 (length-delimited) → tag bytes = field*8 + 2.
        catalog = b"commerce"
        table = b"_dimensions"
        body = (
            bytes([0x0A])  # tag for field 1, wire_type 2
            + bytes([len(catalog)])
            + catalog
            + bytes([0x1A])  # tag for field 3, wire_type 2
            + bytes([len(table)])
            + table
        )
        assert parse_catalog_filter(body) == "commerce"

    def test_parse_catalog_filter_returns_none_when_absent(self) -> None:
        from ob_flight.flight_sql import parse_catalog_filter

        # Only field 3 (table_name_filter_pattern) — no catalog set.
        table = b"_metrics"
        body = bytes([0x1A]) + bytes([len(table)]) + table
        assert parse_catalog_filter(body) is None
