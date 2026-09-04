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
      Net:
        expression: "{Amount} * 0.8"
        abstractType: float

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

    def __call__(self, sql: str, *, dialect: str) -> Any:
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

    def test_projection_names_declared_columns_quoted(self, model: Any, stub: Any) -> None:
        executor = stub(_matching_schema())
        probe_datasource(model, dialect="duckdb")
        assert executor.sql[0] == (
            'SELECT "order_id", "amount", "ordered_at" FROM "main"."orders" LIMIT 0'
        )

    def test_computed_column_is_not_projected(self, model: Any, stub: Any) -> None:
        """``Net`` has an expression, so its ``code`` is not a physical column."""
        executor = stub(_matching_schema())
        probe_datasource(model, dialect="duckdb")
        assert "net" not in executor.sql[0]

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


class TestSkippedObjects:
    def test_nested_object_is_not_probed(self, stub: Any) -> None:
        """Its rows come from unnesting the parent's array column, so its
        columns belong to that array's element type rather than to a table.
        Projecting the declared codes against the fallback ``code`` table would
        report drift that is not there.
        """
        import pyarrow as pa

        from orionbelt.parser.loader import TrackedLoader

        raw, source_map = TrackedLoader().load_string(NESTED_MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors

        executor = stub(FakeResult([("order_id", pa.string()), ("lines", pa.string())]))
        assert probe_datasource(model, dialect="duckdb") == []
        assert len(executor.sql) == 1
        assert "orders" in executor.sql[0]


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
