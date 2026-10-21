"""A measure that omits ``resultType`` takes it from the column it reads.

A count is an integer and a ``min`` of dates a date; a ``sum`` keeps its
column's type and an average is exact over an exact column. The old default
called every one of them a ``float``, which the compiled SQL contradicted and
the result surfaces repeated: a sum of integers was cast to ``DECIMAL(18, 2)``
and overflowed on a value its ``BIGINT`` column held legally.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest
from ob_flight.catalog import model_to_virtual_table_schema

from orionbelt.api.query_cache import build_type_map
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import DataType, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.pgwire.catalog import _measure_sql_type
from orionbelt.service.result_schema import declared_arrow_types, declared_result_schema

MODEL_YAML = """
version: "1.0"
name: inference
{settings}
dataObjects:
  Orders:
    code: orders
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Quantity: {{code: quantity, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
      Weight: {{code: weight, abstractType: float}}
      Rate: {{code: rate, abstractType: decimal, sqlPrecision: 38, sqlScale: 12}}
      Ordered At: {{code: ordered_at, abstractType: date}}
      Status: {{code: status, abstractType: string}}
dimensions:
  Status: {{dataObject: Orders, column: Status, resultType: string}}
measures:
{measures}
"""


def _measure(aggregation: str, column: str | None = None, **extra: str) -> str:
    lines = [f"    aggregation: {aggregation}"]
    if column is not None:
        lines.append(f"    columns: [{{dataObject: Orders, column: {column}}}]")
    lines.extend(f"    {key}: {value}" for key, value in extra.items())
    return "\n".join(lines)


def _model(measures: dict[str, str], settings: str = "") -> SemanticModel:
    body = "\n".join(f"  {name}:\n{spec}" for name, spec in measures.items())
    raw, source_map = TrackedLoader().load_string(
        MODEL_YAML.format(settings=settings, measures=body)
    )
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _inferred(aggregation: str, column: str | None = None, **extra: str) -> DataType:
    model = _model({"M": _measure(aggregation, column, **extra)})
    return model.measures["M"].result_type


class TestInferredResultType:
    @pytest.mark.parametrize(
        ("aggregation", "column", "expected"),
        [
            ("count", "Status", DataType.INT),
            ("count_distinct", "Weight", DataType.INT),
            ("sum", "Quantity", DataType.INT),
            ("sum", "Amount", DataType.DECIMAL),
            ("sum", "Weight", DataType.FLOAT),
            ("min", "Ordered At", DataType.DATE),
            ("max", "Status", DataType.STRING),
            ("any_value", "Amount", DataType.DECIMAL),
            ("mode", "Status", DataType.STRING),
            ("avg", "Quantity", DataType.DECIMAL),
            ("avg", "Amount", DataType.DECIMAL),
            ("avg", "Weight", DataType.FLOAT),
            ("median", "Quantity", DataType.DECIMAL),
            ("median", "Weight", DataType.FLOAT),
            ("listagg", "Status", DataType.STRING),
            ("stddev", "Quantity", DataType.FLOAT),
        ],
    )
    def test_from_the_column(self, aggregation: str, column: str, expected: DataType) -> None:
        assert _inferred(aggregation, column) is expected

    @pytest.mark.parametrize(
        ("aggregation", "column", "expected"),
        [("first", "Quantity", DataType.INT), ("last", "Weight", DataType.FLOAT)],
    )
    def test_first_and_last_keep_the_column_type(
        self, aggregation: str, column: str, expected: DataType
    ) -> None:
        within = "{column: {dataObject: Orders, column: Ordered At}}"
        assert _inferred(aggregation, column, withinGroup=within) is expected

    def test_percentile_cont_over_an_exact_column_is_exact(self) -> None:
        assert _inferred("percentile_cont", "Amount", percentile="0.5") is DataType.DECIMAL

    def test_percentile_disc_keeps_the_column_type(self) -> None:
        assert _inferred("percentile_disc", "Quantity", percentile="0.5") is DataType.INT

    def test_an_expression_over_exact_columns_is_exact(self) -> None:
        expression = "'{[Orders].[Quantity]} * {[Orders].[Amount]}'"
        assert _inferred("sum", expression=expression) is DataType.DECIMAL

    def test_an_expression_reading_a_float_is_a_float(self) -> None:
        expression = "'{[Orders].[Quantity]} * {[Orders].[Weight]}'"
        assert _inferred("sum", expression=expression) is DataType.FLOAT

    def test_a_count_over_an_expression_is_an_integer(self) -> None:
        expression = "'{[Orders].[Weight]}'"
        assert _inferred("count", expression=expression) is DataType.INT

    def test_a_declared_result_type_wins(self) -> None:
        assert _inferred("sum", "Quantity", resultType="float") is DataType.FLOAT


class TestAnIntegerSumIsAnInteger:
    def test_compiles_to_bigint(self) -> None:
        model = _model({"Units": _measure("sum", "Quantity")})
        query = QueryObject.model_validate({"select": {"measures": ["Units"]}})
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert "BIGINT" in sql
        assert "DECIMAL(18, 2)" not in sql

    def test_a_sum_past_sixteen_digits_is_returned_exactly(self) -> None:
        """``DECIMAL(18, 2)`` holds sixteen integer digits; the column holds nineteen."""
        model = _model({"Units": _measure("sum", "Quantity")})
        query = QueryObject.model_validate({"select": {"measures": ["Units"]}})
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        con = duckdb.connect()
        con.execute("CREATE TABLE orders (id VARCHAR, quantity BIGINT)")
        con.execute("INSERT INTO orders VALUES ('a', 100000000000000000), ('b', 3)")
        assert con.execute(sql).fetchone() == (100000000000000003,)


class TestResultSurfacesAgreeWithTheSQL:
    MEASURES = {
        "Units": _measure("sum", "Quantity"),
        "First Order": _measure("min", "Ordered At"),
        "Orders Counted": _measure("count", "Order Id"),
    }

    @pytest.mark.parametrize(
        "settings", ["", "settings: {defaultNumericDataType: 'decimal(20, 4)'}"]
    )
    def test_arrow_types(self, settings: str) -> None:
        model = _model(self.MEASURES, settings)
        query = QueryObject.model_validate({"select": {"measures": list(self.MEASURES)}})
        schema = declared_result_schema(query, model)
        assert schema.field("Units").type == pa.int64()
        assert schema.field("First Order").type == pa.date32()
        assert schema.field("Orders Counted").type == pa.int64()
        types = declared_arrow_types(model)
        assert types["Units"] == pa.int64()
        assert types["First Order"] == pa.date32()

    def test_a_governed_sum_of_decimals_keeps_its_width(self) -> None:
        settings = "settings: {defaultNumericDataType: 'decimal(20, 4)'}"
        model = _model({"Revenue": _measure("sum", "Amount")}, settings)
        query = QueryObject.model_validate({"select": {"measures": ["Revenue"]}})
        assert declared_result_schema(query, model).field("Revenue").type == pa.decimal128(20, 4)

    @pytest.mark.parametrize(
        "settings", ["", "settings: {defaultNumericDataType: 'decimal(20, 4)'}"]
    )
    def test_pgwire_catalog_and_row_description_types(self, settings: str) -> None:
        model = _model(self.MEASURES, settings)
        for label, sql_type in [
            ("Units", "BIGINT"),
            ("First Order", "DATE"),
            ("Orders Counted", "BIGINT"),
        ]:
            assert _measure_sql_type(model.measures[label], model.settings) == sql_type
        hints = build_type_map(model)
        assert (hints["Units"], hints["First Order"]) == ("number", "datetime")


class TestTheDeclaredTypeWins:
    """What the compiler casts to, or the column an uncast aggregate returns."""

    def _schemas(self, model: SemanticModel, label: str) -> list[pa.DataType]:
        query = QueryObject.model_validate({"select": {"measures": [label]}})
        return [
            declared_result_schema(query, model).field(label).type,
            declared_arrow_types(model)[label],
            model_to_virtual_table_schema(model).field(label).type,
        ]

    def test_a_min_of_a_wide_decimal_keeps_the_column_width(self) -> None:
        model = _model({"Lowest Rate": _measure("min", "Rate")})
        for arrow_type in self._schemas(model, "Lowest Rate"):
            assert arrow_type == pa.decimal128(38, 12)
            # The value the engine returns fits the advertised type.
            pa.array([Decimal("0.123456789012")], pa.decimal128(38, 12)).cast(arrow_type)

    def test_an_explicit_double_over_an_int_column_is_a_double(self) -> None:
        model = _model({"Units": _measure("sum", "Quantity", dataType="double")})
        assert model.measures["Units"].result_type is DataType.INT
        for arrow_type in self._schemas(model, "Units"):
            assert arrow_type == pa.float64()
            pa.array([1.8e19]).cast(arrow_type)
        assert _measure_sql_type(model.measures["Units"], model.settings) == "DOUBLE"

    @pytest.mark.parametrize(
        ("data_type", "arrow_type", "sql_type"),
        [
            ("bigint", pa.int64(), "BIGINT"),
            ("decimal(30, 6)", pa.decimal128(30, 6), "DECIMAL(30, 6)"),
            ("string", pa.utf8(), "VARCHAR"),
        ],
    )
    def test_every_explicit_data_type(
        self, data_type: str, arrow_type: pa.DataType, sql_type: str
    ) -> None:
        model = _model({"M": _measure("max", "Weight", dataType=f"'{data_type}'")})
        assert self._schemas(model, "M") == [arrow_type] * 3
        assert _measure_sql_type(model.measures["M"], model.settings) == sql_type
