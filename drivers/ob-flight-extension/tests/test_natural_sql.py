"""End-to-end smoke tests for the Flight natural-SQL surface.

Tests use the real OrionBelt parser/compiler/translator but mock the
SessionManager so no warehouse round-trip happens. Covers:

* virtual table listed in catalog
* CMD_GET_COLUMNS returns dim/measure/metric labels
* Semantic QL → translated → compiled (no DB)
* raw SQL rejected when flag off
* GROUP BY silently ignored in semantic mode
* measure reference in WHERE routed to HAVING

Spec: design/PLAN_flight_natural_sql.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.flight as flight
import pytest

from ob_flight.catalog import (
    model_to_flight_infos,
    model_to_virtual_table_schema,
    model_virtual_table_name,
)
from ob_flight.flight_sql import build_columns_table, build_tables_table
from ob_flight.server import OBFlightServer
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# Inline a small OBML model — independent of the main project's fixtures.
_MODEL_YAML = """\
version: 1.0
dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Order Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string
measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""


@pytest.fixture
def model():
    loader = TrackedLoader()
    raw, source_map = loader.load_string(_MODEL_YAML)
    resolver = ReferenceResolver()
    m, result = resolver.resolve(raw, source_map)
    assert result.valid
    # The server stamps _ob_model_id on the model after pulling it from
    # the SessionManager — mirror that here so the virtual-table name
    # resolver returns the BI-friendly default the tests expect.
    m.__dict__["_ob_model_id"] = "sample_model"
    return m


def _make_server(model: object) -> OBFlightServer:
    """Build a server with a SessionManager mock that returns ``model``.

    The mock returns ``model_id="sample_model"`` so the server's
    ``_get_model`` stamps the same virtual-table id the fixture uses.

    No governance kwargs: v2.4.0+ removed the FLIGHT_ALLOW_* flags. Raw
    SQL is always rejected; write operations are always rejected;
    catalog queries are always routed to model-backed responses.
    """
    store = MagicMock()
    store.list_models.return_value = [MagicMock(model_id="sample_model")]
    store.get_model.return_value = model
    mgr = MagicMock()
    mgr.get_store.return_value = store

    # Avoid binding a real gRPC socket — the tests only exercise the
    # translation/classification methods.
    server = OBFlightServer.__new__(OBFlightServer)
    server._session_manager = mgr
    server._default_dialect = "duckdb"
    server._batch_size = 1024
    import threading

    server._lock = threading.Lock()
    server._pending = {}
    server._prepared = {}
    server._pending_ttl = 300
    return server


# --- catalog ------------------------------------------------------------------


class TestVirtualTableInCatalog:
    def test_virtual_table_is_first_entry(self, model) -> None:
        infos = model_to_flight_infos(model, "default")
        assert infos[0].descriptor.path[-1] == b"sample_model"

    def test_data_objects_hidden(self, model) -> None:
        """Data objects are never exposed in v2.4.0 — the virtual table is
        the only queryable surface."""
        infos = model_to_flight_infos(model, "default")
        labels = {info.descriptor.path[-1] for info in infos}
        assert b"Customers" not in labels
        assert b"Orders" not in labels

    def test_virtual_table_schema_lists_dims_and_measures(self, model) -> None:
        schema = model_to_virtual_table_schema(model)
        names = [f.name for f in schema]
        assert "Customer Country" in names
        assert "Total Revenue" in names


class TestBuildTablesTable:
    def test_lists_virtual_table_first(self, model) -> None:
        t = build_tables_table(model)
        table_names = t.column("table_name").to_pylist()
        assert table_names[0] == "sample_model"
        # v2.4.0+: data objects are never exposed.
        assert "Customers" not in table_names
        assert "Orders" not in table_names

    def test_metadata_views_have_view_type(self, model) -> None:
        t = build_tables_table(model)
        rows = list(
            zip(
                t.column("table_name").to_pylist(),
                t.column("table_type").to_pylist(),
                strict=True,
            )
        )
        for name, type_ in rows:
            if name in {"_dimensions", "_measures", "_metrics"}:
                assert type_ == "VIEW"


class TestBuildColumnsTable:
    def test_includes_dim_and_measure(self, model) -> None:
        t = build_columns_table(model)
        cols = t.column("column_name").to_pylist()
        assert "Customer Country" in cols
        assert "Total Revenue" in cols

    def test_data_object_columns_hidden(self, model) -> None:
        """v2.4.0+: data-object physical columns are never exposed."""
        t = build_columns_table(model)
        tables = set(t.column("table_name").to_pylist())
        assert "Customers" not in tables
        assert "Orders" not in tables


# --- translator + governance --------------------------------------------------


class TestClassifySQL:
    def test_virtual_table_is_semantic(self, model) -> None:
        server = _make_server(model)
        mode = server._classify_sql(
            'SELECT "Customer Country", "Total Revenue" FROM sample_model', model
        )
        assert mode == "semantic"

    def test_data_object_label_rejected(self, model) -> None:
        """v2.4.0+: FROM <data-object-label> is no longer a distinct mode —
        it rejects as raw."""
        server = _make_server(model)
        mode = server._classify_sql('SELECT * FROM "Customers"', model)
        assert mode == "rejected"

    def test_raw_target_rejected(self, model) -> None:
        server = _make_server(model)
        mode = server._classify_sql("SELECT 1 FROM other_thing", model)
        assert mode == "rejected"

    def test_information_schema_is_catalog(self, model) -> None:
        server = _make_server(model)
        mode = server._classify_sql("SELECT * FROM information_schema.tables", model)
        assert mode == "catalog"

    def test_pg_catalog_is_catalog(self, model) -> None:
        server = _make_server(model)
        mode = server._classify_sql("SELECT * FROM pg_catalog.pg_class", model)
        assert mode == "catalog"

    def test_show_tables_is_catalog(self, model) -> None:
        server = _make_server(model)
        mode = server._classify_sql("SHOW TABLES", model)
        assert mode == "catalog"

    def test_describe_is_catalog(self, model) -> None:
        server = _make_server(model)
        mode = server._classify_sql("DESCRIBE sample_model", model)
        assert mode == "catalog"

    def test_select_one_is_catalog(self, model) -> None:
        """Bare SELECT 1 — connectivity probe, never reaches warehouse."""
        server = _make_server(model)
        assert server._classify_sql("SELECT 1", model) == "catalog"

    def test_select_version_is_catalog(self, model) -> None:
        server = _make_server(model)
        assert server._classify_sql("SELECT version()", model) == "catalog"

    def test_virtual_metadata_table_is_catalog(self, model) -> None:
        server = _make_server(model)
        assert server._classify_sql("SELECT * FROM _dimensions", model) == "catalog"
        assert server._classify_sql("SELECT * FROM _measures", model) == "catalog"
        assert server._classify_sql("SELECT * FROM _metrics", model) == "catalog"


class TestPrepareSQL:
    def test_semantic_compiles(self, model) -> None:
        server = _make_server(model)
        sql, dialect, _m, schema, mode = server._prepare_sql(
            'SELECT "Customer Country", "Total Revenue" FROM sample_model'
        )
        assert mode == "semantic"
        assert "SELECT" in sql.upper()
        assert dialect == "duckdb"
        assert schema is not None
        names = [f.name for f in schema]
        assert "Customer Country" in names
        assert "Total Revenue" in names

    def test_semantic_group_by_ignored(self, model) -> None:
        server = _make_server(model)
        sql, *_ = server._prepare_sql(
            'SELECT "Customer Country", "Total Revenue" FROM sample_model '
            'GROUP BY "Customer Country"'
        )
        # The translator silently drops the explicit GROUP BY and the
        # planner re-injects it from the SELECT dims.
        assert "GROUP BY" in sql.upper()

    def test_semantic_measure_in_where_routes_to_having(self, model) -> None:
        server = _make_server(model)
        sql, *_ = server._prepare_sql(
            'SELECT "Customer Country", "Total Revenue" FROM sample_model '
            'WHERE "Total Revenue" > 1000'
        )
        assert "HAVING" in sql.upper()

    def test_raw_sql_always_rejected(self, model) -> None:
        """No flag to bypass — raw SQL never reaches the warehouse."""
        server = _make_server(model)
        with pytest.raises(flight.FlightServerError, match="RAW_SQL_REJECTED"):
            server._prepare_sql("SELECT * FROM warehouse_only_table")

    def test_data_object_label_always_rejected(self, model) -> None:
        """v2.4.0+: FROM-<data-object-label> rejects, no flag to enable it."""
        server = _make_server(model)
        with pytest.raises(flight.FlightServerError, match="RAW_SQL_REJECTED"):
            server._prepare_sql('SELECT * FROM "Customers"')

    def test_translator_error_surfaces_as_flight_error(self, model) -> None:
        server = _make_server(model)
        with pytest.raises(
            flight.FlightServerError, match="OrionBelt Semantic QL translation failed"
        ):
            server._prepare_sql('SELECT "Bogus" FROM sample_model')

    def test_catalog_query_routes_to_catalog_mode(self, model) -> None:
        """information_schema / SHOW / DESCRIBE return mode=catalog."""
        server = _make_server(model)
        _sql, _d, _m, _schema, mode = server._prepare_sql("SELECT * FROM information_schema.tables")
        assert mode == "catalog"

    def test_show_tables_routes_to_catalog_mode(self, model) -> None:
        server = _make_server(model)
        _sql, _d, _m, _schema, mode = server._prepare_sql("SHOW TABLES")
        assert mode == "catalog"


class TestWriteOperationBlocking:
    """v2.4.0+: DDL / DML / TCL never reach the warehouse."""

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO sample_model VALUES (1)",
            "UPDATE \"Customers\" SET name = 'x'",
            'DELETE FROM "Customers"',
            "DROP TABLE sample_model",
            "CREATE TABLE x (a INT)",
            "ALTER TABLE x ADD COLUMN b INT",
            "TRUNCATE TABLE x",
        ],
    )
    def test_write_operations_rejected(self, model, sql: str) -> None:
        server = _make_server(model)
        with pytest.raises(flight.FlightServerError, match="WRITE_OPERATION_REJECTED"):
            server._prepare_sql(sql)


class TestCatalogHandling:
    """Catalog queries answered from the model — never touch the warehouse."""

    def test_show_tables_lists_virtual_table(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SHOW TABLES", model)
        # tables-table schema includes table_name; virtual table must appear
        names = table.column("table_name").to_pylist()
        assert "sample_model" in names

    def test_information_schema_tables_returns_data(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SELECT * FROM information_schema.tables", model)
        assert "table_name" in [f.name for f in table.schema]
        assert "sample_model" in table.column("table_name").to_pylist()

    def test_information_schema_columns_returns_data(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SELECT * FROM information_schema.columns", model)
        cols = table.column("column_name").to_pylist()
        assert "Customer Country" in cols
        assert "Total Revenue" in cols

    def test_select_one_returns_canned_value(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SELECT 1", model)
        # SELECT 1 → one row, one column, value "1"
        assert table.num_rows == 1

    def test_select_version_returns_obsl_brand(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SELECT version()", model)
        values = table.column(0).to_pylist()
        assert any("OrionBelt" in v for v in values)

    def test_select_current_schema(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SELECT current_schema()", model)
        assert table.num_rows == 1

    def test_select_dimensions_virtual_table(self, model) -> None:
        server = _make_server(model)
        table = server._handle_catalog_sql("SELECT * FROM _dimensions", model)
        # _dimensions virtual table — should contain the model's dims
        names = table.column("name").to_pylist()
        assert "Customer Country" in names

    def test_unknown_probe_returns_empty(self, model) -> None:
        server = _make_server(model)
        # Made-up system function — never seen
        table = server._handle_catalog_sql("SELECT some_unknown_func()", model)
        assert table.num_rows == 1  # but value is empty string
        assert table.column(0)[0].as_py() == ""


class TestSchemaProbeShortcut:
    def test_semantic_query_returns_arrow_schema_without_db(self, model) -> None:
        server = _make_server(model)
        _sql, _d, _m, schema, _mode = server._prepare_sql(
            'SELECT "Customer Country", "Total Revenue" FROM sample_model '
            "WHERE \"Customer Country\" = 'US'"
        )
        # Result schema should be inferred from the model — no DB needed.
        assert schema is not None
        assert schema.field(0).name == "Customer Country"
        assert schema.field(1).name == "Total Revenue"

    def test_rollup_adds_grouping_flag_columns(self, model) -> None:
        server = _make_server(model)
        _sql, _d, _m, schema, _mode = server._prepare_sql(
            'SELECT "Customer Country", "Total Revenue" FROM sample_model WITH ROLLUP'
        )
        assert schema is not None
        names = [f.name for f in schema]
        assert "_g_Customer Country" in names
