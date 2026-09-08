"""Tests for Flight catalog (model -> FlightInfo conversion)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from ob_flight.catalog import (
    build_dimensions_data,
    build_measures_data,
    build_metrics_data,
    model_to_flight_infos,
    object_to_schema,
)


class TestObjectToSchema:
    def test_basic_columns(self):
        col1 = MagicMock()
        col1.label = "Region"
        col1.abstract_type = "string"
        col2 = MagicMock()
        col2.label = "Amount"
        col2.abstract_type = "float"

        obj = MagicMock()
        obj.columns = {"Region": col1, "Amount": col2}

        schema = object_to_schema(obj)
        assert len(schema) == 2
        assert schema.field(0).name == "Region"
        assert schema.field(0).type == pa.utf8()
        assert schema.field(1).name == "Amount"
        assert schema.field(1).type == pa.float64()

    def test_int_type(self):
        col = MagicMock()
        col.label = "Count"
        col.abstract_type = "int"
        obj = MagicMock()
        obj.columns = {"Count": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.int64()

    def test_datetime_type(self):
        col = MagicMock()
        col.label = "Created"
        col.abstract_type = "datetime"
        obj = MagicMock()
        obj.columns = {"Created": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.timestamp("us")

    def test_unknown_type_fallback(self):
        col = MagicMock()
        col.label = "Data"
        col.abstract_type = "custom_type"
        obj = MagicMock()
        obj.columns = {"Data": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.utf8()

    def test_no_columns(self):
        obj = MagicMock()
        obj.columns = {}
        schema = object_to_schema(obj)
        assert len(schema) == 0

    def test_no_columns_attr(self):
        obj = MagicMock(spec=[])  # no attributes
        schema = object_to_schema(obj)
        assert len(schema) == 0

    def test_none_abstract_type_defaults_to_string(self):
        col = MagicMock()
        col.label = "Name"
        col.abstract_type = None
        obj = MagicMock()
        obj.columns = {"Name": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.utf8()

    def test_boolean_type(self):
        col = MagicMock()
        col.label = "Active"
        col.abstract_type = "boolean"
        obj = MagicMock()
        obj.columns = {"Active": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.bool_()

    def test_date_type(self):
        col = MagicMock()
        col.label = "OrderDate"
        col.abstract_type = "date"
        obj = MagicMock()
        obj.columns = {"OrderDate": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.date32()

    def test_timestamp_type(self):
        col = MagicMock()
        col.label = "Modified"
        col.abstract_type = "timestamp"
        obj = MagicMock()
        obj.columns = {"Modified": col}
        schema = object_to_schema(obj)
        assert schema.field(0).type == pa.timestamp("us")

    def test_label_fallback_to_col_name(self):
        col = MagicMock()
        col.label = None
        col.abstract_type = "string"
        obj = MagicMock()
        obj.columns = {"my_column": col}
        schema = object_to_schema(obj)
        assert schema.field(0).name == "my_column"


def _make_model_with_dim(name: str = "sales_model") -> MagicMock:
    """Build a model mock with one dim + one measure → produces a non-empty
    virtual table schema. See PLAN_flight_natural_sql.md §3.5."""
    dim = MagicMock()
    dim.label = "Region"
    dim.result_type = MagicMock(value="string")
    dim.time_grain = None
    dim.description = None
    dim.column = "region"
    dim.view = "Sales"

    meas = MagicMock()
    meas.label = "Total Sales"
    meas.aggregation = "sum"
    meas.expression = None
    meas.result_type = MagicMock(value="float")
    meas.columns = []
    meas.description = None

    col = MagicMock()
    col.label = "X"
    col.abstract_type = MagicMock(value="string")
    obj = MagicMock()
    obj.columns = {"X": col}

    model = MagicMock()
    model.label = name
    model.id = name
    model.name = name
    model.data_objects = {"Sales": obj}
    model.dimensions = {"Region": dim}
    model.measures = {"Total Sales": meas}
    model.metrics = {}
    return model


class TestModelToFlightInfos:
    def test_default_hides_data_objects(self):
        """Default: vt + label views (non-empty only) + 3 metadata views.

        Test model has 1 dim + 1 measure + 0 metrics → 6 entries:
        ``sales_model`` (vt), ``dimensions``, ``measures`` (label views;
        ``metrics`` skipped because empty), and the 3 ``_*_metadata``
        introspection views.
        """
        model = _make_model_with_dim()
        infos = model_to_flight_infos(model, "test-model")
        assert len(infos) == 6
        # First info is the semantic virtual table
        assert infos[0].descriptor.path == [b"test-model", b"sales_model"]

    def test_expose_data_objects(self):
        """expose_data_objects=True: vt + Sales + label views + 3 metadata views."""
        model = _make_model_with_dim()
        infos = model_to_flight_infos(model, "test-model", expose_data_objects=True)
        # vt + Sales + (dimensions, measures) + 3 metadata = 7
        assert len(infos) == 7
        labels = {info.descriptor.path[-1] for info in infos}
        assert b"sales_model" in labels
        assert b"Sales" in labels

    def test_no_data_objects(self):
        model = MagicMock()
        model.data_objects = {}
        infos = model_to_flight_infos(model, "m1")
        assert len(infos) == 0

    def test_no_data_objects_attr(self):
        model = MagicMock(spec=[])
        infos = model_to_flight_infos(model, "m1")
        assert len(infos) == 0

    def test_virtual_tables_included(self):
        """Label views appear when non-empty; metadata views always appear."""
        model = _make_model_with_dim()
        infos = model_to_flight_infos(model, "m1")
        vt_paths = {info.descriptor.path[-1] for info in infos}
        # Label views (the model has dims + measures but no metrics, so
        # ``metrics`` is skipped — empty views aren't advertised).
        assert b"dimensions" in vt_paths
        assert b"measures" in vt_paths
        # Metadata views — always present regardless of model contents.
        assert b"_dimensions_metadata" in vt_paths
        assert b"_measures_metadata" in vt_paths
        assert b"_metrics_metadata" in vt_paths

    def test_virtual_table_schema_has_dims_and_measures(self):
        """The semantic virtual table exposes dims + measures + metrics."""
        from ob_flight.catalog import model_to_virtual_table_schema

        model = _make_model_with_dim()
        schema = model_to_virtual_table_schema(model)
        names = [f.name for f in schema]
        assert "Region" in names
        assert "Total Sales" in names


class TestBuildDimensionsData:
    def test_basic(self):
        dim = MagicMock()
        dim.label = "Region"
        dim.view = "Orders"
        dim.column = "region"
        dim.result_type = MagicMock(value="string")
        dim.time_grain = None
        dim.description = "Sales region"

        model = MagicMock()
        model.dimensions = {"Region": dim}

        table = build_dimensions_data(model)
        assert len(table) == 1
        assert table.column("name")[0].as_py() == "Region"
        assert table.column("data_object")[0].as_py() == "Orders"
        assert table.column("column")[0].as_py() == "region"
        assert table.column("type")[0].as_py() == "string"
        assert table.column("description")[0].as_py() == "Sales region"

    def test_with_time_grain(self):
        dim = MagicMock()
        dim.label = "Order Date"
        dim.view = "Orders"
        dim.column = "order_date"
        dim.result_type = MagicMock(value="date")
        dim.time_grain = MagicMock(value="month")
        dim.description = None

        model = MagicMock()
        model.dimensions = {"Order Date": dim}

        table = build_dimensions_data(model)
        assert table.column("time_grain")[0].as_py() == "month"

    def test_empty_model(self):
        model = MagicMock()
        model.dimensions = {}
        table = build_dimensions_data(model)
        assert len(table) == 0

    def test_no_dimensions_attr(self):
        model = MagicMock(spec=[])
        table = build_dimensions_data(model)
        assert len(table) == 0


class TestBuildMeasuresData:
    def test_basic(self):
        col_ref = MagicMock()
        col_ref.view = "Orders"
        col_ref.column = "amount"

        meas = MagicMock()
        meas.label = "Total Sales"
        meas.aggregation = "sum"
        meas.expression = None
        meas.result_type = MagicMock(value="float")
        meas.columns = [col_ref]
        meas.description = "Sum of sales"

        model = MagicMock()
        model.measures = {"Total Sales": meas}

        table = build_measures_data(model)
        assert len(table) == 1
        assert table.column("name")[0].as_py() == "Total Sales"
        assert table.column("aggregation")[0].as_py() == "sum"
        assert table.column("columns")[0].as_py() == "Orders.amount"
        assert table.column("description")[0].as_py() == "Sum of sales"

    def test_empty_model(self):
        model = MagicMock()
        model.measures = {}
        table = build_measures_data(model)
        assert len(table) == 0


class TestBuildMetricsData:
    def test_basic(self):
        met = MagicMock()
        met.label = "Return Rate"
        met.type = MagicMock(value="derived")
        met.expression = "{[Total Returns]} / {[Total Sales]}"
        met.measure = None
        met.time_dimension = None
        met.window = None
        met.grain_to_date = None
        met.period_over_period = None
        met.description = "Rate of returns"

        model = MagicMock()
        model.metrics = {"Return Rate": met}
        model.dimensions = {}

        table = build_metrics_data(model)
        assert len(table) == 1
        assert table.column("name")[0].as_py() == "Return Rate"
        assert table.column("metric_type")[0].as_py() == "derived"
        assert table.column("expression")[0].as_py() == "{[Total Returns]} / {[Total Sales]}"
        assert table.column("time_dimension")[0].as_py() is None
        assert table.column("time_grain")[0].as_py() is None
        assert table.column("window")[0].as_py() is None
        assert table.column("grain_to_date")[0].as_py() is None

    def test_cumulative_surfaces_time_dimension_window_and_grain(self):
        # The referenced dim declares time_grain=month — that's the unit
        # that disambiguates window=3 as "3 months".
        dim = MagicMock()
        dim.time_grain = MagicMock(value="month")

        met = MagicMock()
        met.label = "Rolling 3m Sales"
        met.type = MagicMock(value="cumulative")
        met.expression = None
        met.measure = "Total Sales"
        met.time_dimension = "Order Month"
        met.window = 3
        met.grain_to_date = None
        met.period_over_period = None
        met.description = None

        model = MagicMock()
        model.metrics = {"Rolling 3m Sales": met}
        model.dimensions = {"Order Month": dim}

        table = build_metrics_data(model)
        assert table.column("metric_type")[0].as_py() == "cumulative"
        assert table.column("measure")[0].as_py() == "Total Sales"
        assert table.column("time_dimension")[0].as_py() == "Order Month"
        assert table.column("time_grain")[0].as_py() == "month"
        assert table.column("window")[0].as_py() == 3

    def test_period_over_period_surfaces_nested_time_dimension_and_grain(self):
        # PoP carries its own explicit grain — independent of the dim.
        pop = MagicMock()
        pop.time_dimension = "Order Date"
        pop.grain = MagicMock(value="year")

        met = MagicMock()
        met.label = "YoY Sales"
        met.type = MagicMock(value="period_over_period")
        met.expression = "{[Total Sales]}"
        met.measure = None
        met.time_dimension = None
        met.window = None
        met.grain_to_date = None
        met.period_over_period = pop
        met.description = None

        model = MagicMock()
        model.metrics = {"YoY Sales": met}
        model.dimensions = {}

        table = build_metrics_data(model)
        assert table.column("metric_type")[0].as_py() == "period_over_period"
        assert table.column("time_dimension")[0].as_py() == "Order Date"
        assert table.column("time_grain")[0].as_py() == "year"

    def test_empty_model(self):
        model = MagicMock()
        model.metrics = {}
        table = build_metrics_data(model)
        assert len(table) == 0


class TestAdvertisedSchemaMatchesStream:
    """``get_flight_info`` must advertise exactly what ``do_get`` streams.

    A client reads FlightInfo before fetching. JDBC treats a mismatch as an
    empty response (silent); ADBC refuses the endpoint outright. Both
    catalog defects found by the ADBC conformance harness were mismatches
    of this kind:

    * every command advertised a placeholder ``result: utf8`` while
      streaming its real table;
    * ``GetExportedKeys`` / ``GetCrossReference`` streamed the six-column
      primary-key shape where FlightSql.proto defines the shared
      thirteen-column foreign-key one.

    Asserting the invariant directly is cheaper than covering each command
    through a client, and it holds for commands no client here exercises.
    """

    def test_every_catalog_command_streams_its_advertised_schema(self) -> None:
        import threading

        from ob_flight.server import _CATALOG_COMMAND_SCHEMAS, OBFlightServer
        from ob_flight.server_catalog import build_catalog_table

        server = OBFlightServer.__new__(OBFlightServer)
        server._session_manager = None
        server._default_dialect = "duckdb"
        server._lock = threading.Lock()
        server._pending = {}
        server._prepared = {}
        server._pending_ttl = 300
        server._batch_size = 1024
        server._cache = None
        server._cache_config = None

        mismatches = []
        for type_url, advertised in _CATALOG_COMMAND_SCHEMAS.items():
            streamed = build_catalog_table(server, type_url).schema
            if streamed != advertised:
                mismatches.append(
                    f"{type_url.rsplit('.', 1)[-1]}:\n"
                    f"  advertised {advertised}\n"
                    f"  streamed   {streamed}"
                )
        assert not mismatches, "\n".join(mismatches)

    def test_foreign_key_commands_share_one_schema(self) -> None:
        """FlightSql.proto gives all three the same thirteen columns.

        Only ``GetPrimaryKeys`` uses the six-column shape.
        """
        from ob_flight.flight_sql import FOREIGN_KEYS_SCHEMA, PRIMARY_KEYS_SCHEMA
        from ob_flight.server import _CATALOG_COMMAND_SCHEMAS

        fk_commands = [
            u
            for u in _CATALOG_COMMAND_SCHEMAS
            if u.endswith(("ImportedKeys", "ExportedKeys", "CrossReference"))
        ]
        assert len(fk_commands) == 3, fk_commands
        for url in fk_commands:
            assert _CATALOG_COMMAND_SCHEMAS[url] is FOREIGN_KEYS_SCHEMA, url

        assert len(FOREIGN_KEYS_SCHEMA) == 13
        assert len(PRIMARY_KEYS_SCHEMA) == 6
        assert FOREIGN_KEYS_SCHEMA.names[:4] == [
            "pk_catalog_name",
            "pk_db_schema_name",
            "pk_table_name",
            "pk_column_name",
        ]


class TestCatalogWhereFiltering:
    """A catalog WHERE used to be accepted and discarded.

    ``handle_catalog_sql`` dispatched on the FROM target and returned a whole
    canned view, so ``WHERE table_name = 'x'`` returned every table and the
    client was told they all matched. Worse than refusing, because nothing
    said the predicate had been dropped.
    """

    def _table(self) -> object:
        import pyarrow as pa

        return pa.table(
            {
                "table_name": ["model", "_dimensions_metadata", "orders"],
                "table_type": ["TABLE", "VIEW", "TABLE"],
                "column_size": [1, 2, 3],
            }
        )

    def _filter(self, sql: str) -> tuple[object, bool]:
        import sqlglot

        from ob_flight.server_catalog import filter_catalog_table

        return filter_catalog_table(self._table(), sqlglot.parse_one(sql))

    def test_equality_selects_one_row(self) -> None:
        table, applied = self._filter("SELECT * FROM t WHERE table_name = 'model'")
        assert applied is True
        assert table.column("table_name").to_pylist() == ["model"]

    def test_a_column_may_be_on_either_side(self) -> None:
        left, _ = self._filter("SELECT * FROM t WHERE table_name = 'model'")
        right, _ = self._filter("SELECT * FROM t WHERE 'model' = table_name")
        assert left.num_rows == right.num_rows == 1

    def test_names_match_case_insensitively(self) -> None:
        """Clients spell catalog columns both ways; the canned tables are lower."""
        table, applied = self._filter("SELECT * FROM t WHERE TABLE_NAME = 'model'")
        assert applied is True
        assert table.num_rows == 1

    def test_like_and_in_and_not(self) -> None:
        assert self._filter("SELECT * FROM t WHERE table_name LIKE 'mod%'")[0].num_rows == 1
        assert (
            self._filter("SELECT * FROM t WHERE table_name IN ('model', 'orders')")[0].num_rows == 2
        )
        assert self._filter("SELECT * FROM t WHERE NOT table_name = 'model'")[0].num_rows == 2

    def test_and_or_combine(self) -> None:
        both = self._filter("SELECT * FROM t WHERE table_type = 'TABLE' AND table_name = 'orders'")[
            0
        ]
        assert both.column("table_name").to_pylist() == ["orders"]
        either = self._filter(
            "SELECT * FROM t WHERE table_name = 'model' OR table_name = 'orders'"
        )[0]
        assert either.num_rows == 2

    def test_comparison_operators(self) -> None:
        assert self._filter("SELECT * FROM t WHERE column_size > 1")[0].num_rows == 2
        assert self._filter("SELECT * FROM t WHERE column_size <= 1")[0].num_rows == 1

    def test_a_predicate_it_cannot_evaluate_leaves_the_table_alone(self) -> None:
        """The behaviour that was there before, so nothing working today stops.

        The caller is told, and the prepared path uses that to refuse a
        parameter rather than bind one into a predicate nobody can apply.
        """
        table, applied = self._filter("SELECT * FROM t WHERE weird(table_name) = 1")
        assert applied is False
        assert table.num_rows == 3

    def test_a_subquery_is_not_evaluated(self) -> None:
        table, applied = self._filter("SELECT * FROM t WHERE table_name IN (SELECT x FROM y)")
        assert applied is False
        assert table.num_rows == 3

    def test_no_where_is_no_filter(self) -> None:
        table, applied = self._filter("SELECT * FROM t")
        assert applied is True
        assert table.num_rows == 3

    def test_a_false_conjunct_settles_an_unknown_one(self) -> None:
        """``FALSE AND <unknown>`` is False in SQL, so the row is excluded and
        the filter still counts as applied."""
        table, applied = self._filter(
            "SELECT * FROM t WHERE table_name = '__none__' AND weird(x) = 1"
        )
        assert applied is True
        assert table.num_rows == 0


class TestCatalogProjection:
    """A catalog SELECT list used to be discarded along with the WHERE.

    The dispatch answered with a whole canned view, so a client asking for one
    column received four. Harmless to a tolerant client and wrong to a strict
    one - and the advertised schema is built from these columns, so it is the
    schema that was wide too.
    """

    def _table(self) -> object:
        import pyarrow as pa

        return pa.table(
            {
                "catalog_name": ["orionbelt", "orionbelt"],
                "table_name": ["model", "orders"],
                "table_type": ["TABLE", "VIEW"],
            }
        )

    def _project(self, sql: str) -> tuple[object, bool]:
        import sqlglot

        from ob_flight.server_catalog import project_catalog_table

        return project_catalog_table(self._table(), sqlglot.parse_one(sql))

    def test_one_column_is_one_column(self) -> None:
        table, applied = self._project("SELECT table_name FROM t")
        assert applied is True
        assert table.column_names == ["table_name"]

    def test_columns_keep_the_order_asked_for(self) -> None:
        table, _ = self._project("SELECT table_type, table_name FROM t")
        assert table.column_names == ["table_type", "table_name"]

    def test_an_alias_renames(self) -> None:
        """That is the name the client will look for."""
        table, applied = self._project("SELECT table_name AS name FROM t")
        assert applied is True
        assert table.column_names == ["name"]

    def test_names_match_case_insensitively(self) -> None:
        table, applied = self._project("SELECT TABLE_NAME FROM t")
        assert applied is True
        assert table.column_names == ["table_name"]

    def test_star_is_the_whole_view(self) -> None:
        table, applied = self._project("SELECT * FROM t")
        assert applied is False
        assert len(table.column_names) == 3

    def test_an_unknown_column_leaves_the_view_whole(self) -> None:
        """Rather than an empty or partial projection: the advertised schema is
        built from these columns, and one that disagrees with the stream is
        worse than a wide one."""
        table, applied = self._project("SELECT no_such_column FROM t")
        assert applied is False
        assert len(table.column_names) == 3

    def test_an_expression_leaves_the_view_whole(self) -> None:
        table, applied = self._project("SELECT COUNT(*) FROM t")
        assert applied is False
        assert len(table.column_names) == 3

    def test_filtering_may_use_a_column_the_projection_drops(self) -> None:
        """``SELECT table_name ... WHERE table_type = 'VIEW'`` is ordinary, so
        the filter has to run before the projection throws its column away."""
        import sqlglot

        from ob_flight.server_catalog import _answer_catalog_view

        answered = _answer_catalog_view(
            self._table(),
            sqlglot.parse_one("SELECT table_name FROM t WHERE table_type = 'VIEW'"),
        )
        assert answered.column_names == ["table_name"]
        assert answered.column("table_name").to_pylist() == ["orders"]


#: A real resolved model, not a mock: these tests build genuine Arrow tables
#: from it, and a ``MagicMock`` attribute reaches pyarrow as an object it
#: cannot convert.
_GUARD_MODEL_YAML = """\
version: 1.0
name: sales_model

dataObjects:
  Sales:
    code: sales
    database: ""
    schema: main
    columns:
      Region Code:
        code: region
        abstractType: string
      Amount Col:
        code: amount
        abstractType: float

dimensions:
  Region:
    dataObject: Sales
    column: Region Code
    resultType: string

measures:
  Total Sales:
    aggregation: sum
    columns:
      - dataObject: Sales
        column: Amount Col
    resultType: float
"""


class TestEveryCatalogViewHonoursTheStatement:
    """Parameterised over the entry points, not written per branch.

    ``<model>.model`` shipped skipping both the filter and the projection - and
    reporting the filter as *applied*, so the prepared-statement bind check
    believed it. It was missed because each view is a separate branch of one
    dispatch and the earlier fix went view by view. This asks all of them the
    same three questions, so the next branch added has to answer them too.
    """

    #: Every SELECT-with-a-FROM branch of the dispatch.
    VIEWS = [
        "information_schema.tables",
        "information_schema.columns",
        '"sales_model"."model"',
        "_dimensions_metadata",
        "_measures_metadata",
    ]

    def _model(self) -> Any:
        from orionbelt.parser.loader import TrackedLoader
        from orionbelt.parser.resolver import ReferenceResolver

        raw, source_map = TrackedLoader().load_string(_GUARD_MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def _ask(self, sql: str) -> tuple[Any, bool]:
        from ob_flight.server_catalog import handle_catalog_sql_checked

        return handle_catalog_sql_checked(None, sql, self._model())

    def _first_column(self, view: str) -> str:
        whole, _ = self._ask(f"SELECT * FROM {view}")
        return str(whole.column_names[0])

    @pytest.mark.parametrize("view", VIEWS)
    def test_a_matching_nothing_filter_returns_nothing(self, view: str) -> None:
        column = self._first_column(view)
        table, applied = self._ask(
            f"SELECT {column} FROM {view} WHERE {column} = '__no_such_value__'"
        )
        assert applied is True
        assert table.num_rows == 0, f"{view} ignored its WHERE"

    @pytest.mark.parametrize("view", VIEWS)
    def test_a_projection_narrows(self, view: str) -> None:
        column = self._first_column(view)
        one, _ = self._ask(f"SELECT {column} FROM {view}")
        assert one.column_names == [column], f"{view} ignored its SELECT list"

    @pytest.mark.parametrize("view", VIEWS)
    def test_an_unevaluable_predicate_is_reported(self, view: str) -> None:
        """Claiming ``applied`` when nothing was applied is the worst of the
        three: the bind check believes it and lets a value through."""
        column = self._first_column(view)
        whole, _ = self._ask(f"SELECT * FROM {view}")
        table, applied = self._ask(f"SELECT {column} FROM {view} WHERE weird(x) = 1")
        assert applied is False, f"{view} claimed to apply a predicate it cannot evaluate"
        assert table.num_rows == whole.num_rows


class TestCatalogOrdering:
    """``ORDER BY`` was the third clause accepted and discarded.

    BI tools sort catalog listings, and the view's insertion order came back
    whatever was asked - including ``DESC``, which is the case where a client
    can least tell it was ignored.
    """

    def _table(self) -> Any:
        import pyarrow as pa

        return pa.table(
            {
                "table_name": ["model", "_meta", "orders"],
                "table_type": ["TABLE", "VIEW", "TABLE"],
            }
        )

    def _order(self, sql: str) -> tuple[Any, bool]:
        import sqlglot

        from ob_flight.server_catalog import order_catalog_table

        return order_catalog_table(self._table(), sqlglot.parse_one(sql))

    def _names(self, sql: str) -> list[str]:
        return list(self._order(sql)[0].column("table_name").to_pylist())

    def test_ascending_is_the_default(self) -> None:
        assert self._names("SELECT table_name FROM t ORDER BY table_name") == [
            "_meta",
            "model",
            "orders",
        ]

    def test_descending_reverses_it(self) -> None:
        """The case a client can least tell was ignored: unsorted data often
        looks plausible ascending, never plausible descending."""
        assert self._names("SELECT table_name FROM t ORDER BY table_name DESC") == [
            "orders",
            "model",
            "_meta",
        ]

    def test_an_ordinal_names_the_select_list(self) -> None:
        """``ORDER BY 1`` is the first thing *selected*, not the first column
        of the view - different lists."""
        assert self._names("SELECT table_name FROM t ORDER BY 1") == [
            "_meta",
            "model",
            "orders",
        ]

    def test_an_ordinal_past_the_select_list_is_refused(self) -> None:
        _, applied = self._order("SELECT table_name FROM t ORDER BY 9")
        assert applied is False

    def test_an_alias_sorts_by_what_feeds_it(self) -> None:
        """A bare ``ORDER BY n`` parses as a column, so looking it up in the
        table first found nothing and dropped the whole sort."""
        table, applied = self._order("SELECT table_name AS n FROM t ORDER BY n")
        assert applied is True
        assert table.column("table_name").to_pylist() == ["_meta", "model", "orders"]

    def test_several_keys_apply_in_order(self) -> None:
        assert self._names("SELECT table_name FROM t ORDER BY table_type, table_name DESC") == [
            "orders",
            "model",
            "_meta",
        ]

    def test_a_key_the_projection_drops_still_sorts(self) -> None:
        """Ordering runs before projection, so this is an ordinary ask."""
        import sqlglot

        from ob_flight.server_catalog import answer_catalog_view

        answered, _ = answer_catalog_view(
            self._table(),
            sqlglot.parse_one("SELECT table_name FROM t ORDER BY table_type, table_name"),
        )
        assert answered.column_names == ["table_name"]
        assert answered.column("table_name").to_pylist() == ["model", "orders", "_meta"]

    def test_an_unsupported_term_leaves_the_order_alone(self) -> None:
        """All or nothing: a partly applied sort is a different order from the
        one asked for, and looks like an answer."""
        table, applied = self._order("SELECT table_name FROM t ORDER BY weird(x)")
        assert applied is False
        assert table.column("table_name").to_pylist() == ["model", "_meta", "orders"]

    def test_no_order_by_is_no_sort(self) -> None:
        table, applied = self._order("SELECT table_name FROM t")
        assert applied is True
        assert table.column("table_name").to_pylist() == ["model", "_meta", "orders"]


class TestCatalogLimit:
    """``LIMIT`` / ``OFFSET``, the last of the four discarded clauses."""

    def _table(self) -> Any:
        import pyarrow as pa

        return pa.table({"n": ["a", "b", "c", "d", "e"]})

    def _limit(self, sql: str) -> tuple[Any, bool]:
        import sqlglot

        from ob_flight.server_catalog import limit_catalog_table

        return limit_catalog_table(self._table(), sqlglot.parse_one(sql))

    def _rows(self, sql: str) -> list[str]:
        return list(self._limit(sql)[0].column("n").to_pylist())

    def test_limit_takes_the_first_rows(self) -> None:
        assert self._rows("SELECT n FROM t LIMIT 2") == ["a", "b"]

    def test_offset_skips(self) -> None:
        assert self._rows("SELECT n FROM t OFFSET 3") == ["d", "e"]

    def test_offset_applies_with_limit(self) -> None:
        """Supporting only ``LIMIT`` would answer this with the first two rows -
        the right *number* of the wrong rows, which a client cannot see."""
        assert self._rows("SELECT n FROM t LIMIT 2 OFFSET 2") == ["c", "d"]

    def test_a_limit_past_the_end_is_everything(self) -> None:
        assert self._rows("SELECT n FROM t LIMIT 99") == ["a", "b", "c", "d", "e"]

    def test_limit_zero_is_nothing(self) -> None:
        """Distinct from no limit at all, and a real thing clients send to
        probe a schema."""
        table, applied = self._limit("SELECT n FROM t LIMIT 0")
        assert applied is True
        assert table.num_rows == 0

    def test_no_limit_is_everything(self) -> None:
        table, applied = self._limit("SELECT n FROM t")
        assert applied is True
        assert table.num_rows == 5

    def test_a_non_literal_limit_is_refused(self) -> None:
        table, applied = self._limit("SELECT n FROM t LIMIT (SELECT 2)")
        assert applied is False
        assert table.num_rows == 5

    def test_it_runs_after_the_ordering(self) -> None:
        """A limit over an unsorted table is an arbitrary subset; the order is
        what decides which rows survive."""
        import sqlglot

        from ob_flight.server_catalog import answer_catalog_view

        answered, _ = answer_catalog_view(
            self._table(), sqlglot.parse_one("SELECT n FROM t ORDER BY n DESC LIMIT 2")
        )
        assert answered.column("n").to_pylist() == ["e", "d"]
