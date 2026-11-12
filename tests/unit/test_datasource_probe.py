"""Tests for the online datasource probe (``service/datasource_probe.py``).

The probe's whole job is to turn what a warehouse says into findings, so the
tests drive it through a stub executor that records the SQL it was asked to run
and answers with a chosen schema or a chosen failure. That covers the branch
that matters most - the two-probe fallback that separates a missing table from
a missing column - without needing eight live warehouses, and pins the SQL
shape, which is the part a dialect change could quietly alter.
"""

from __future__ import annotations

from typing import Any

import pytest

from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service import datasource_probe
from orionbelt.service.datasource_probe import probe_datasource
from orionbelt.service.db_executor import ExecutionError, ExecutionUnavailableError

MODEL_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: orders
    database: ""
    schema: main
    columns:
      Order ID:
        code: order_id
        abstractType: string
      Amount:
        code: amount
        abstractType: float
        numClass: additive
      Ordered At:
        code: ordered_at
        abstractType: timestamp

dimensions:
  Order Ref:
    dataObject: Orders
    column: Order ID
    resultType: string

measures:
  Total Amount:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
"""


class FakeColumn:
    """Stands in for ``db_executor.ColumnMeta``."""

    def __init__(self, name: str, type_hint: str = "string") -> None:
        self.name = name
        self.type_hint = type_hint


class FakeResult:
    """Stands in for ``db_executor.ExecutionResult``.

    ``arrow_schema`` is built from real PyArrow fields so the family mapping is
    exercised against the types a driver actually hands back, rather than
    against a second reimplementation of it.
    """

    def __init__(self, fields: list[tuple[str, Any]]) -> None:
        import pyarrow as pa

        self.arrow_schema = pa.schema([pa.field(name, typ) for name, typ in fields])
        self.columns = [FakeColumn(name) for name, _ in fields]


class FakeExecutor:
    """Answers each probe from a table of SQL-substring -> outcome."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.sql: list[str] = []

    def __call__(self, sql: str, *, dialect: str, source: Any = None) -> Any:
        self.sql.append(sql)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def model() -> Any:
    """The probe model, resolved."""

    from orionbelt.parser.loader import TrackedLoader

    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake ``execute_sql`` and hand back the installer."""

    def install(*outcomes: Any) -> FakeExecutor:
        executor = FakeExecutor(list(outcomes))
        monkeypatch.setattr("orionbelt.service.db_executor.execute_sql", executor, raising=True)
        return executor

    return install


def _matching_schema() -> FakeResult:
    import pyarrow as pa

    return FakeResult(
        [
            ("order_id", pa.string()),
            ("amount", pa.float64()),
            ("ordered_at", pa.timestamp("us")),
        ]
    )


class TestHappyPath:
    def test_matching_model_reports_nothing(self, model: Any, stub: Any) -> None:
        stub(_matching_schema())
        assert probe_datasource(model, dialect="duckdb") == []

    def test_one_round_trip_when_everything_matches(self, model: Any, stub: Any) -> None:
        """The star probe is the fallback, so a clean model must not pay for it."""
        executor = stub(_matching_schema())
        probe_datasource(model, dialect="duckdb")
        assert len(executor.sql) == 1

    def test_projection_reads_columns_as_a_query_does(self, model: Any, stub: Any) -> None:
        """Quoted and qualified by the data object's alias, as codegen spells them."""
        executor = stub(_matching_schema())
        probe_datasource(model, dialect="duckdb")
        assert executor.sql[0] == (
            'SELECT "Orders"."order_id" AS "c1", "Orders"."amount" AS "c2", '
            '"Orders"."ordered_at" AS "c3"\n'
            'FROM "main"."orders" AS "Orders"\n'
            "LIMIT 0"
        )

    def test_probe_scans_nothing(self, model: Any, stub: Any) -> None:
        executor = stub(_matching_schema())
        probe_datasource(model, dialect="duckdb")
        assert executor.sql[0].endswith("LIMIT 0")


class TestMissingTable:
    def test_both_probes_failing_reports_the_table(self, model: Any, stub: Any) -> None:
        stub(
            ExecutionError("relation orders does not exist"),
            ExecutionError("relation orders does not exist"),
        )
        findings = probe_datasource(model, dialect="duckdb")
        assert [f.code for f in findings] == ["DATASOURCE_TABLE_MISSING"]

    def test_message_carries_the_driver_error(self, model: Any, stub: Any) -> None:
        stub(ExecutionError("boom"), ExecutionError("relation orders does not exist"))
        (finding,) = probe_datasource(model, dialect="duckdb")
        assert "relation orders does not exist" in finding.message

    def test_path_points_at_the_code(self, model: Any, stub: Any) -> None:
        stub(ExecutionError("boom"), ExecutionError("boom"))
        (finding,) = probe_datasource(model, dialect="duckdb")
        assert finding.path == "dataObjects.Orders.code"


class TestMissingColumn:
    def test_star_probe_names_the_missing_column(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            ExecutionError('column "amount" does not exist'),
            FakeResult([("order_id", pa.string()), ("ordered_at", pa.timestamp("us"))]),
        )
        findings = probe_datasource(model, dialect="duckdb")
        assert [f.code for f in findings] == ["DATASOURCE_COLUMN_MISSING"]
        assert findings[0].context is not None
        assert findings[0].context["code"] == "amount"

    def test_second_probe_is_the_star(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        executor = stub(
            ExecutionError("nope"),
            FakeResult([("order_id", pa.string()), ("ordered_at", pa.timestamp("us"))]),
        )
        probe_datasource(model, dialect="duckdb")
        assert executor.sql[1] == 'SELECT * FROM "main"."orders" LIMIT 0'


class TestColumnCase:
    def test_case_only_difference_is_reported_as_case(self, model: Any, stub: Any) -> None:
        """The projection was rejected, so this engine does care about case."""
        import pyarrow as pa

        stub(
            ExecutionError('column "amount" does not exist'),
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("AMOUNT", pa.float64()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            ),
        )
        findings = probe_datasource(model, dialect="postgres")
        assert [f.code for f in findings] == ["DATASOURCE_COLUMN_CASE"]

    def test_case_finding_hints_the_real_spelling(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            ExecutionError("nope"),
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("AMOUNT", pa.float64()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            ),
        )
        (finding,) = probe_datasource(model, dialect="postgres")
        assert finding.hint is not None
        assert "AMOUNT" in finding.hint

    def test_case_insensitive_engine_reports_nothing(self, model: Any, stub: Any) -> None:
        """DuckDB accepts the projection, so the probe never asks about case.

        This is the reason the probe projects the declared names rather than
        diffing a catalog listing: on an engine that folds case, a spelling
        difference is not drift, and a diff would report one.
        """
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("AMOUNT", pa.float64()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        assert probe_datasource(model, dialect="duckdb") == []


class TestTypeMismatch:
    def test_declared_float_backed_by_text(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.string()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        findings = probe_datasource(model, dialect="duckdb")
        assert [f.code for f in findings] == ["DATASOURCE_TYPE_MISMATCH"]
        assert findings[0].path == "dataObjects.Orders.columns.Amount.abstractType"

    def test_declared_timestamp_backed_by_text(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.float64()),
                    ("ordered_at", pa.string()),
                ]
            )
        )
        (finding,) = probe_datasource(model, dialect="duckdb")
        assert finding.context is not None
        assert finding.context["declaredType"] == "timestamp"
        assert finding.context["actualFamily"] == "string"

    @pytest.mark.parametrize("arrow_type", ["int64", "float32", "decimal"])
    def test_any_numeric_satisfies_float(self, model: Any, stub: Any, arrow_type: str) -> None:
        """The comparison is by family: a widened int is not drift."""
        import pyarrow as pa

        actual = {
            "int64": pa.int64(),
            "float32": pa.float32(),
            "decimal": pa.decimal128(38, 9),
        }[arrow_type]
        stub(
            FakeResult(
                [("order_id", pa.string()), ("amount", actual), ("ordered_at", pa.timestamp("us"))]
            )
        )
        assert probe_datasource(model, dialect="duckdb") == []

    def test_date_satisfies_declared_timestamp(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.float64()),
                    ("ordered_at", pa.date32()),
                ]
            )
        )
        assert probe_datasource(model, dialect="duckdb") == []

    def test_null_typed_column_is_not_compared(self, model: Any, stub: Any) -> None:
        """A ``LIMIT 0`` fetch is exactly where a null-typed column turns up.

        It carries no type to disagree with, so comparing it would report every
        column of a driver that degrades an empty result as a mismatch.
        """
        import pyarrow as pa

        stub(
            FakeResult(
                [("order_id", pa.string()), ("amount", pa.null()), ("ordered_at", pa.null())]
            )
        )
        assert probe_datasource(model, dialect="duckdb") == []

    def test_coarse_string_hint_never_refutes(self, model: Any, stub: Any) -> None:
        """Without an Arrow schema, ``"string"`` is also the unknown bucket.

        ``db_executor`` falls back to it for every PEP 249 type code it does
        not recognise, so treating it as a claim would fail a correct model.
        """

        class NoArrowResult:
            arrow_schema = None
            columns = [
                FakeColumn("order_id", "string"),
                FakeColumn("amount", "string"),
                FakeColumn("ordered_at", "string"),
            ]

        stub(NoArrowResult())
        assert probe_datasource(model, dialect="duckdb") == []

    def test_coarse_number_hint_does_refute(self, model: Any, stub: Any) -> None:
        """The other three buckets are positive classifications and are used."""

        class NoArrowResult:
            arrow_schema = None
            columns = [
                FakeColumn("order_id", "number"),
                FakeColumn("amount", "number"),
                FakeColumn("ordered_at", "datetime"),
            ]

        stub(NoArrowResult())
        findings = probe_datasource(model, dialect="duckdb")
        assert [f.code for f in findings] == ["DATASOURCE_TYPE_MISMATCH"]
        assert findings[0].context is not None
        assert findings[0].context["column"] == "Order ID"


class TestUnavailable:
    def test_missing_connection_reports_once(self, model: Any, stub: Any) -> None:
        stub(ExecutionUnavailableError("no credentials for postgres"))
        findings = probe_datasource(model, dialect="postgres")
        assert [f.code for f in findings] == ["DATASOURCE_UNAVAILABLE"]

    def test_unavailable_stops_further_probes(self, model: Any, stub: Any) -> None:
        """Every remaining object would fail identically."""
        executor = stub(ExecutionUnavailableError("no credentials"))
        probe_datasource(model, dialect="postgres")
        assert len(executor.sql) == 1

    def test_unsupported_dialect_opens_no_connection(self, model: Any, stub: Any) -> None:
        executor = stub()
        findings = probe_datasource(model, dialect="oracle")
        assert [f.code for f in findings] == ["DATASOURCE_UNSUPPORTED_DIALECT"]
        assert executor.sql == []


COMPUTED_MODEL_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: orders
    database: ""
    schema: main
    columns:
      Order ID:
        code: order_id
        abstractType: string
      Amount:
        code: amount
        abstractType: float
        numClass: additive
      Net:
        expression: "{Amount} * 0.8"
        abstractType: float

dimensions:
  Order Ref:
    dataObject: Orders
    column: Order ID
    resultType: string
"""


def _resolve(yaml: str) -> Any:
    from orionbelt.parser.loader import TrackedLoader

    raw, source_map = TrackedLoader().load_string(yaml)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _physical_schema() -> FakeResult:
    import pyarrow as pa

    return FakeResult([("c1", pa.string()), ("c2", pa.float64())])


def _computed_schema(net: Any = None) -> FakeResult:
    import pyarrow as pa

    return FakeResult([("c1", pa.string()), ("c2", pa.float64()), ("c3", net or pa.float64())])


def _one_column(arrow_type: Any) -> FakeResult:
    return FakeResult([("c1", arrow_type)])


class TestComputedColumns:
    """A computed column is read as its expression, in the same statement."""

    def test_read_in_the_same_round_trip(self, stub: Any) -> None:
        executor = stub(_computed_schema())
        assert probe_datasource(_resolve(COMPUTED_MODEL_YAML), dialect="duckdb") == []
        assert len(executor.sql) == 1
        assert '"Orders"."amount" * 0.8 AS "c3"' in executor.sql[0]

    def test_a_result_of_another_type_names_the_expression(self, stub: Any) -> None:
        import pyarrow as pa

        stub(_computed_schema(net=pa.string()))
        (finding,) = probe_datasource(_resolve(COMPUTED_MODEL_YAML), dialect="duckdb")
        assert finding.code == "DATASOURCE_TYPE_MISMATCH"
        assert finding.path == "dataObjects.Orders.columns.Net.abstractType"
        assert "Computed column 'Net'" in finding.message
        assert finding.hint is not None
        assert "expression" in finding.hint

    def test_an_expression_the_engine_rejects(self, stub: Any) -> None:
        """The physical columns read alone, so the expression is what failed."""
        executor = stub(
            ExecutionError("No function matches the given name"),
            _physical_schema(),
            ExecutionError("No function matches the given name"),
        )
        (finding,) = probe_datasource(_resolve(COMPUTED_MODEL_YAML), dialect="duckdb")
        assert finding.code == "DATASOURCE_EXPRESSION_FAILED"
        assert finding.path == "dataObjects.Orders.columns.Net.expression"
        assert "No function matches the given name" in finding.message
        assert "order_id" not in executor.sql[2]
        assert "0.8" in executor.sql[2]

    def test_a_missing_column_and_the_expression_reading_it(self, stub: Any) -> None:
        import pyarrow as pa

        stub(
            ExecutionError('column "amount" does not exist'),
            ExecutionError('column "amount" does not exist'),
            FakeResult([("order_id", pa.string())]),
            ExecutionError('column "amount" does not exist'),
        )
        findings = probe_datasource(_resolve(COMPUTED_MODEL_YAML), dialect="duckdb")
        assert [f.code for f in findings] == [
            "DATASOURCE_COLUMN_MISSING",
            "DATASOURCE_EXPRESSION_FAILED",
        ]

    def test_a_missing_table_is_reported_once(self, stub: Any) -> None:
        """No expression can be read from a table that is not there."""
        executor = stub(
            ExecutionError("relation orders does not exist"),
            ExecutionError("relation orders does not exist"),
            ExecutionError("relation orders does not exist"),
        )
        findings = probe_datasource(_resolve(COMPUTED_MODEL_YAML), dialect="duckdb")
        assert [f.code for f in findings] == ["DATASOURCE_TABLE_MISSING"]
        assert len(executor.sql) == 3

    def test_parts_that_read_alone_but_not_together(self, stub: Any) -> None:
        import pyarrow as pa

        stub(ExecutionError("statement too complex"), _physical_schema(), _one_column(pa.float64()))
        (finding,) = probe_datasource(_resolve(COMPUTED_MODEL_YAML), dialect="duckdb")
        assert finding.code == "DATASOURCE_PROBE_FAILED"
        assert "statement too complex" in finding.message

    def test_an_expression_reading_another_object_is_left_out(self, stub: Any) -> None:
        """A query joins the other object first; the probe reads this one alone."""
        import pyarrow as pa

        yaml = """\
version: 1.0
dataObjects:
  Orders:
    code: orders
    database: ""
    schema: main
    columns:
      Order ID: {code: order_id, abstractType: string}
      Customer ID: {code: customer_id, abstractType: string}
      Customer Label: {expression: "{[Customers].[Customer Name]}", abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer ID]
        columnsTo: [Customer ID]
  Customers:
    code: customers
    database: ""
    schema: main
    columns:
      Customer ID: {code: customer_id, abstractType: string}
      Customer Name: {code: name, abstractType: string}
"""
        two = FakeResult([("c1", pa.string()), ("c2", pa.string())])
        executor = stub(two, two)
        assert probe_datasource(_resolve(yaml), dialect="duckdb") == []
        assert "Customers" not in executor.sql[0]


NESTED_MODEL_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: orders
    database: ""
    schema: main
    columns:
      Order ID:
        code: order_id
        abstractType: string
      Lines:
        code: lines
        abstractType: json

  Order Lines:
    code: lines
    database: ""
    schema: main
    nestedIn:
      dataObject: Orders
      column: Lines
    columns:
      Sku:
        code: sku
        abstractType: string

dimensions:
  Order Ref:
    dataObject: Orders
    column: Order ID
    resultType: string
"""


def _orders_schema() -> FakeResult:
    import pyarrow as pa

    return FakeResult([("c1", pa.string()), ("c2", pa.string())])


class TestNestedObjects:
    """A ``nestedIn`` object is read the way a query reads it: unnested out of its
    parent's array column, not from the fallback ``code`` table.
    """

    def test_fields_are_read_from_the_parents_array(self, stub: Any) -> None:
        import pyarrow as pa

        executor = stub(_orders_schema(), _one_column(pa.string()))
        assert probe_datasource(_resolve(NESTED_MODEL_YAML), dialect="duckdb") == []
        assert len(executor.sql) == 2
        lines_sql = executor.sql[1]
        assert 'FROM "main"."orders" AS "Orders"' in lines_sql
        assert 'UNNEST("Orders"."Lines")' in lines_sql
        assert '"main"."lines"' not in lines_sql

    def test_a_field_the_elements_lack(self, stub: Any) -> None:
        import pyarrow as pa

        stub(
            _orders_schema(),
            ExecutionError('Could not find key "sku" in struct'),
            _one_column(pa.int32()),
            ExecutionError('Could not find key "sku" in struct'),
        )
        (finding,) = probe_datasource(_resolve(NESTED_MODEL_YAML), dialect="duckdb")
        assert finding.code == "DATASOURCE_COLUMN_MISSING"
        assert finding.path == "dataObjects.Order Lines.columns.Sku.code"
        assert 'Could not find key "sku"' in finding.message

    def test_an_array_column_that_cannot_be_unnested(self, stub: Any) -> None:
        import pyarrow as pa

        stub(
            _orders_schema(),
            ExecutionError("UNNEST requires a list"),
            ExecutionError("UNNEST requires a list"),
            _one_column(pa.int32()),
        )
        (finding,) = probe_datasource(_resolve(NESTED_MODEL_YAML), dialect="duckdb")
        assert finding.code == "DATASOURCE_UNNEST_FAILED"
        assert finding.path == "dataObjects.Order Lines.nestedIn.column"

    def test_an_unreadable_parent_is_reported_on_the_parent_only(self, stub: Any) -> None:
        missing = ExecutionError("relation orders does not exist")
        stub(missing, missing, missing, missing, missing)
        findings = probe_datasource(_resolve(NESTED_MODEL_YAML), dialect="duckdb")
        assert [(f.code, f.path) for f in findings] == [
            ("DATASOURCE_TABLE_MISSING", "dataObjects.Orders.code")
        ]

    def test_without_a_from_clause_unnest_the_code_table_is_read(self, stub: Any) -> None:
        """Dremio has no FROM-clause unnest, so its queries read ``code``; so does the probe."""
        import pyarrow as pa

        executor = stub(_orders_schema(), _one_column(pa.string()))
        assert probe_datasource(_resolve(NESTED_MODEL_YAML), dialect="dremio") == []
        assert 'FROM "main"."lines" AS "Order Lines"' in executor.sql[1]

    def test_without_an_unnest_or_a_table_nothing_is_read(self, stub: Any) -> None:
        yaml = NESTED_MODEL_YAML.replace(
            '  Order Lines:\n    code: lines\n    database: ""\n    schema: main\n',
            "  Order Lines:\n",
        )
        executor = stub(_orders_schema())
        assert probe_datasource(_resolve(yaml), dialect="dremio") == []
        assert len(executor.sql) == 1


class TestProbeFailed:
    def test_projection_failing_for_no_visible_reason(self, model: Any, stub: Any) -> None:
        """Table readable, every column present and correctly spelled, yet the
        projection was rejected - a column-level grant, most likely. The probe
        cannot name the cause, so it returns the driver's words.
        """

        stub(ExecutionError("permission denied for column amount"), _matching_schema())
        findings = probe_datasource(model, dialect="postgres")
        assert [f.code for f in findings] == ["DATASOURCE_PROBE_FAILED"]
        assert "permission denied for column amount" in findings[0].message

    def test_unexpected_driver_error_does_not_lose_the_model(self, model: Any, stub: Any) -> None:
        stub(RuntimeError("driver exploded"))
        findings = probe_datasource(model, dialect="duckdb")
        assert [f.code for f in findings] == ["DATASOURCE_PROBE_FAILED"]


class TestValidateIntegration:
    """``ModelStore.validate`` is where a finding becomes an error."""

    def test_offline_by_default_opens_no_connection(self, monkeypatch: Any) -> None:
        from orionbelt.service.model_store import ModelStore

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("validate must not touch the datasource by default")

        monkeypatch.setattr("orionbelt.service.db_executor.execute_sql", explode)
        assert ModelStore().validate(MODEL_YAML).valid is True

    def test_online_finding_makes_the_model_invalid(self, stub: Any) -> None:
        from orionbelt.service.model_store import ModelStore

        stub(ExecutionError("nope"), ExecutionError("relation orders does not exist"))
        summary = ModelStore().validate(MODEL_YAML, datasource_dialect="duckdb")
        assert summary.valid is False
        assert [e.code for e in summary.errors] == ["DATASOURCE_TABLE_MISSING"]

    def test_online_clean_model_stays_valid(self, stub: Any) -> None:
        from orionbelt.service.model_store import ModelStore

        stub(_matching_schema())
        assert ModelStore().validate(MODEL_YAML, datasource_dialect="duckdb").valid is True

    def test_offline_errors_still_come_first(self, stub: Any) -> None:
        """A model that does not parse is not worth a round trip's diagnosis."""
        from orionbelt.service.model_store import ModelStore

        executor = stub()
        summary = ModelStore().validate("key: [unclosed", datasource_dialect="duckdb")
        assert summary.valid is False
        assert executor.sql == []


class TestOpaqueAndIntervalTypes:
    """Types PyArrow cannot represent natively still have to be classified.

    ADBC's Postgres driver wraps NUMERIC and MONEY in an ``OpaqueType`` whose
    storage is a string, and every ``pa.types.is_*`` helper answers False for
    it. Structural classification alone therefore skips exactly the columns a
    measure is most likely to be declared over. Verified against a live
    Postgres: `numeric` arrives opaque, `interval` arrives as a native
    `month_day_nano_interval`, and before this both were unclassified.
    """

    def test_opaque_numeric_is_a_number(self) -> None:
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="numeric", vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) == "number"

    def test_opaque_money_is_a_number(self) -> None:
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="money", vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) == "number"

    def test_opaque_interval_is_datetime(self) -> None:
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="interval", vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) == "datetime"

    def test_native_interval_is_datetime(self) -> None:
        """Not opaque at all - ADBC returns a real Arrow interval."""
        import pyarrow as pa

        assert datasource_probe._arrow_family(pa.month_day_nano_interval()) == "datetime"
        assert datasource_probe._arrow_family(pa.duration("s")) == "datetime"

    def test_unrecognised_opaque_name_refutes_nothing(self) -> None:
        """``coarse_hint_from_type_name`` answers "string" for a real text type
        and for everything it does not recognise alike, so it must not be
        allowed to contradict a declaration."""
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="tsvector", vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) is None

    def test_declared_string_over_postgres_numeric_is_reported(self, model: Any, stub: Any) -> None:
        """The finding this fixes, end to end: `Order ID` is declared string."""
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.opaque(pa.string(), "numeric", "PostgreSQL")),
                    ("amount", pa.float64()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        findings = probe_datasource(model, dialect="postgres")
        assert [f.code for f in findings] == ["DATASOURCE_TYPE_MISMATCH"]
        assert findings[0].context is not None
        assert findings[0].context["actualFamily"] == "number"

    def test_declared_float_over_postgres_numeric_is_clean(self, model: Any, stub: Any) -> None:
        """The other half: a NUMERIC behind a `float` must not now false-fire."""
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.opaque(pa.string(), "numeric", "PostgreSQL")),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        assert probe_datasource(model, dialect="postgres") == []


class TestStringFamilyExtensionTypes:
    """A driver has two ways to hand back a type PyArrow cannot spell natively,
    and they need opposite treatment. Verified against a live Postgres:
    ``json``/``jsonb`` arrive as the canonical ``arrow.json`` extension over
    string storage, while ``uuid``/``xml``/``inet`` arrive as ``arrow.opaque``
    with only a vendor name to go on.
    """

    def test_canonical_json_extension_is_a_string(self) -> None:
        """Postgres json and jsonb both arrive this way."""
        import pyarrow as pa

        assert datasource_probe._arrow_family(pa.json_(pa.string())) == "string"

    @pytest.mark.parametrize("name", ["uuid", "xml", "inet", "json", "text", "varchar"])
    def test_named_opaque_text_types_are_strings(self, name: str) -> None:
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name=name, vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) == "string"

    def test_opaque_name_matching_is_case_insensitive(self) -> None:
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="UUID", vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) == "string"

    def test_unknown_opaque_name_stays_undecidable(self) -> None:
        """``tsvector`` is text-ish, but the mapping cannot *say* so, and its
        storage is a carrier rather than a claim. Silence beats a guess."""
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="tsvector", vendor_name="PostgreSQL")
        assert datasource_probe._arrow_family(opaque) is None

    def test_opaque_numeric_is_not_read_as_its_string_storage(self) -> None:
        """The reason opaque storage is never consulted: ADBC carries NUMERIC
        over string storage, so a storage-based answer would call money text.
        """
        import pyarrow as pa

        opaque = pa.opaque(pa.string(), type_name="numeric", vendor_name="PostgreSQL")
        assert opaque.storage_type == pa.string()
        assert datasource_probe._arrow_family(opaque) == "number"

    def test_declared_float_over_a_json_column_is_reported(self, model: Any, stub: Any) -> None:
        """End to end: OBML maps json to the string family, so a float declared
        over a Postgres JSON column is drift and must not pass."""
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.json_(pa.string())),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        findings = probe_datasource(model, dialect="postgres")
        assert [f.code for f in findings] == ["DATASOURCE_TYPE_MISMATCH"]
        assert findings[0].context is not None
        assert findings[0].context["actualFamily"] == "string"

    def test_declared_float_over_an_opaque_uuid_is_reported(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.opaque(pa.string(), "uuid", "PostgreSQL")),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        findings = probe_datasource(model, dialect="postgres")
        assert [f.code for f in findings] == ["DATASOURCE_TYPE_MISMATCH"]

    def test_declared_json_over_a_json_column_is_clean(self, stub: Any) -> None:
        """OBML ``json`` sits in the string family, so this must not fire."""
        import pyarrow as pa

        from orionbelt.parser.loader import TrackedLoader

        yaml = MODEL_YAML.replace(
            "      Amount:\n        code: amount\n        abstractType: float",
            "      Amount:\n        code: amount\n        abstractType: json",
        ).replace("aggregation: sum", "aggregation: count")
        raw, source_map = TrackedLoader().load_string(yaml)
        resolved, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.json_(pa.string())),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        assert probe_datasource(resolved, dialect="postgres") == []


class TestTypeMismatchHint:
    """The hint has to name the family that came back, not the declared one.

    Naming the declared family reads as "change float to a number type" -
    advice to make no change at all - which is what it did before.
    """

    def test_hint_names_the_actual_family_not_the_declared_one(self, model: Any, stub: Any) -> None:
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.string()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        (finding,) = probe_datasource(model, dialect="duckdb")
        assert finding.hint is not None
        assert "change abstractType to a 'string' type" in finding.hint
        assert "'number'" not in finding.hint

    def test_hint_offers_the_cast_back_to_the_declared_type(self, model: Any, stub: Any) -> None:
        """The other half of the choice: fix the model, or fix the source."""
        import pyarrow as pa

        stub(
            FakeResult(
                [
                    ("order_id", pa.string()),
                    ("amount", pa.string()),
                    ("ordered_at", pa.timestamp("us")),
                ]
            )
        )
        (finding,) = probe_datasource(model, dialect="duckdb")
        assert finding.hint is not None
        assert "cast 'amount' to float" in finding.hint
