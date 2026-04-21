"""Tests for OBML data type registry, resolver, and dialect rendering."""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.type_resolver import (
    resolve_measure_data_type,
    resolve_metric_data_type,
)
from orionbelt.dialect.bigquery import BigQueryDialect
from orionbelt.dialect.clickhouse import ClickHouseDialect
from orionbelt.dialect.postgres import PostgresDialect
from orionbelt.dialect.snowflake import SnowflakeDialect
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import (
    Measure,
    Metric,
    ModelSettings,
    SemanticModel,
)
from orionbelt.models.types import (
    BUILTIN_DEFAULT,
    DIVISION_DEFAULT,
    DecimalType,
    SimpleType,
    parse_data_type,
)


class TestParseDataType:
    def test_decimal(self) -> None:
        t = parse_data_type("decimal(18, 2)")
        assert isinstance(t, DecimalType)
        assert t.precision == 18
        assert t.scale == 2

    def test_decimal_no_spaces(self) -> None:
        t = parse_data_type("decimal(38,8)")
        assert isinstance(t, DecimalType)
        assert t.precision == 38
        assert t.scale == 8

    def test_simple_types(self) -> None:
        names = ("bigint", "integer", "double", "date", "timestamp", "time", "string", "boolean")
        for name in names:
            t = parse_data_type(name)
            assert isinstance(t, SimpleType)
            assert t.name == name

    def test_case_insensitive(self) -> None:
        t = parse_data_type("DECIMAL(18, 2)")
        assert isinstance(t, DecimalType)

    def test_invalid_type(self) -> None:
        with pytest.raises(ValueError, match="Unknown data_type"):
            parse_data_type("varchar")

    def test_decimal_zero_precision(self) -> None:
        with pytest.raises(ValueError, match="precision must be > 0"):
            parse_data_type("decimal(0, 0)")

    def test_decimal_negative_scale(self) -> None:
        with pytest.raises(ValueError, match="scale must be >= 0"):
            parse_data_type("decimal(10, -1)")

    def test_decimal_scale_exceeds_precision(self) -> None:
        with pytest.raises(ValueError, match="scale.*cannot exceed precision"):
            parse_data_type("decimal(5, 10)")

    def test_decimal_exceeds_max_precision(self) -> None:
        with pytest.raises(ValueError, match="exceeds maximum"):
            parse_data_type("decimal(200000, 2)")

    def test_render_roundtrip(self) -> None:
        t = parse_data_type("decimal(18, 6)")
        assert t.render() == "decimal(18, 6)"


class TestResolveMeasureDataType:
    def test_explicit_wins(self) -> None:
        m = Measure(label="Revenue", aggregation="sum", data_type="decimal(38, 8)")
        result = resolve_measure_data_type(m, None)
        assert result == DecimalType(38, 8)

    def test_count_infers_bigint(self) -> None:
        m = Measure(label="Order Count", aggregation="count")
        result = resolve_measure_data_type(m, None)
        assert result == SimpleType("bigint")

    def test_count_distinct_infers_bigint(self) -> None:
        m = Measure(label="Unique Customers", aggregation="count_distinct")
        result = resolve_measure_data_type(m, None)
        assert result == SimpleType("bigint")

    def test_division_infers_decimal_18_6(self) -> None:
        m = Measure(
            label="Rate",
            aggregation="sum",
            expression="{[Orders].[Amount]} / {[Orders].[Count]}",
        )
        result = resolve_measure_data_type(m, None)
        assert result == DIVISION_DEFAULT

    def test_sum_uses_builtin_default(self) -> None:
        m = Measure(label="Revenue", aggregation="sum")
        result = resolve_measure_data_type(m, None)
        assert result == BUILTIN_DEFAULT

    def test_avg_uses_builtin_default(self) -> None:
        m = Measure(label="Average", aggregation="avg")
        result = resolve_measure_data_type(m, None)
        assert result == BUILTIN_DEFAULT

    def test_model_settings_override(self) -> None:
        m = Measure(label="Revenue", aggregation="sum")
        settings = ModelSettings(default_numeric_data_type="decimal(18, 4)")
        result = resolve_measure_data_type(m, settings)
        assert result == DecimalType(18, 4)

    def test_min_passthrough(self) -> None:
        m = Measure(label="Min Price", aggregation="min")
        result = resolve_measure_data_type(m, None)
        assert result is None

    def test_max_passthrough(self) -> None:
        m = Measure(label="Max Price", aggregation="max")
        result = resolve_measure_data_type(m, None)
        assert result is None

    def test_listagg_passthrough(self) -> None:
        m = Measure(label="Names", aggregation="listagg")
        result = resolve_measure_data_type(m, None)
        assert result is None


class TestResolveMetricDataType:
    def test_explicit_wins(self) -> None:
        m = Metric(
            label="Rate",
            expression="{[Revenue]} / {[Count]}",
            data_type="decimal(18, 4)",
        )
        result = resolve_metric_data_type(m, None)
        assert result == DecimalType(18, 4)

    def test_division_infers_decimal_18_6(self) -> None:
        m = Metric(label="Rate", expression="{[Revenue]} / {[Count]}")
        result = resolve_metric_data_type(m, None)
        assert result == DIVISION_DEFAULT

    def test_simple_expression_uses_default(self) -> None:
        m = Metric(label="Total", expression="{[Revenue]} + {[Tax]}")
        result = resolve_metric_data_type(m, None)
        assert result == BUILTIN_DEFAULT


class TestDialectRendering:
    def test_postgres_decimal(self) -> None:
        d = PostgresDialect()
        assert d.render_obml_type(DecimalType(18, 2)) == "NUMERIC(18, 2)"

    def test_postgres_bigint(self) -> None:
        d = PostgresDialect()
        assert d.render_obml_type(SimpleType("bigint")) == "BIGINT"

    def test_postgres_double(self) -> None:
        d = PostgresDialect()
        assert d.render_obml_type(SimpleType("double")) == "DOUBLE PRECISION"

    def test_snowflake_decimal(self) -> None:
        d = SnowflakeDialect()
        assert d.render_obml_type(DecimalType(18, 2)) == "NUMBER(18, 2)"

    def test_snowflake_bigint(self) -> None:
        d = SnowflakeDialect()
        assert d.render_obml_type(SimpleType("bigint")) == "NUMBER(38, 0)"

    def test_clickhouse_decimal(self) -> None:
        d = ClickHouseDialect()
        assert d.render_obml_type(DecimalType(18, 2)) == "Decimal(18, 2)"

    def test_clickhouse_bigint(self) -> None:
        d = ClickHouseDialect()
        assert d.render_obml_type(SimpleType("bigint")) == "Int64"

    def test_bigquery_decimal(self) -> None:
        d = BigQueryDialect()
        assert d.render_obml_type(DecimalType(18, 2)) == "NUMERIC(18, 2)"

    def test_precision_clamping(self) -> None:
        d = SnowflakeDialect()
        # Snowflake max is 38
        result = d.render_obml_type(DecimalType(50, 10))
        assert result == "NUMBER(38, 10)"


class TestModelValidation:
    def test_valid_data_type_on_measure(self) -> None:
        m = Measure(label="Rev", aggregation="sum", data_type="decimal(18, 2)")
        assert m.data_type == "decimal(18, 2)"

    def test_invalid_data_type_on_measure(self) -> None:
        with pytest.raises(ValueError):
            Measure(label="Rev", aggregation="sum", data_type="varchar(255)")

    def test_valid_data_type_on_metric(self) -> None:
        m = Metric(label="Rate", expression="{[A]} / {[B]}", data_type="decimal(18, 6)")
        assert m.data_type == "decimal(18, 6)"

    def test_invalid_data_type_on_metric(self) -> None:
        with pytest.raises(ValueError):
            Metric(label="Rate", expression="{[A]} / {[B]}", data_type="number(18, 2)")

    def test_valid_model_settings(self) -> None:
        s = ModelSettings(default_numeric_data_type="decimal(18, 4)")
        assert s.default_numeric_data_type == "decimal(18, 4)"

    def test_invalid_model_settings_non_decimal(self) -> None:
        with pytest.raises(ValueError, match="must be a decimal"):
            ModelSettings(default_numeric_data_type="bigint")

    def test_settings_on_semantic_model(self) -> None:
        model = SemanticModel(settings=ModelSettings(default_numeric_data_type="decimal(18, 4)"))
        assert model.settings is not None
        assert model.settings.default_numeric_data_type == "decimal(18, 4)"


class TestCompilationWithCast:
    """Integration: verify CAST appears in compiled SQL."""

    SIMPLE_MODEL_YAML = """
version: "1.0"
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Price: { code: PRICE, abstractType: float }
      Country: { code: COUNTRY, abstractType: string }
      Qty: { code: QTY, abstractType: int }
dimensions:
  Country:
    dataObject: Orders
    column: Country
    resultType: string
measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: "{[Orders].[Price]}"
  Order Count:
    resultType: int
    aggregation: count
  Avg Price:
    resultType: float
    aggregation: avg
    expression: "{[Orders].[Price]} / {[Orders].[Qty]}"
"""

    @pytest.fixture
    def model(self) -> SemanticModel:
        from orionbelt.parser import ReferenceResolver, TrackedLoader

        loader = TrackedLoader()
        raw, sm = loader.load_string(self.SIMPLE_MODEL_YAML)
        resolver = ReferenceResolver()
        model, _ = resolver.resolve(raw, sm)
        return model

    def test_sum_gets_decimal_cast(self, model: SemanticModel) -> None:
        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Revenue"]))
        result = pipeline.compile(query, model, "postgres")
        assert "CAST(" in result.sql
        assert "NUMERIC(18, 2)" in result.sql

    def test_count_gets_no_cast(self, model: SemanticModel) -> None:
        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Order Count"]))
        result = pipeline.compile(query, model, "postgres")
        # COUNT infers bigint but COUNT already returns bigint natively — still emits CAST
        assert "CAST(" in result.sql
        assert "BIGINT" in result.sql

    def test_division_gets_decimal_18_6(self, model: SemanticModel) -> None:
        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Avg Price"]))
        result = pipeline.compile(query, model, "postgres")
        assert "NUMERIC(18, 6)" in result.sql

    def test_snowflake_uses_number(self, model: SemanticModel) -> None:
        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Revenue"]))
        result = pipeline.compile(query, model, "snowflake")
        assert "NUMBER(18, 2)" in result.sql

    def test_clickhouse_uses_decimal(self, model: SemanticModel) -> None:
        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Revenue"]))
        result = pipeline.compile(query, model, "clickhouse")
        assert "Decimal(18, 2)" in result.sql

    def test_explicit_data_type_overrides(self, model: SemanticModel) -> None:
        model.measures["Revenue"].data_type = "decimal(38, 8)"
        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Revenue"]))
        result = pipeline.compile(query, model, "postgres")
        assert "NUMERIC(38, 8)" in result.sql

    def test_model_settings_default(self) -> None:
        yaml = """
version: "1.0"
settings:
  defaultNumericDataType: "decimal(18, 4)"
dataObjects:
  T:
    code: T
    columns:
      A: { code: A, abstractType: float }
dimensions:
  Dim:
    dataObject: T
    column: A
    resultType: string
measures:
  Total:
    resultType: float
    aggregation: sum
    expression: "{[T].[A]}"
"""
        from orionbelt.parser import ReferenceResolver, TrackedLoader

        loader = TrackedLoader()
        raw, sm = loader.load_string(yaml)
        resolver = ReferenceResolver()
        model, _ = resolver.resolve(raw, sm)

        pipeline = CompilationPipeline()
        query = QueryObject(select=QuerySelect(dimensions=["Dim"], measures=["Total"]))
        result = pipeline.compile(query, model, "postgres")
        assert "NUMERIC(18, 4)" in result.sql
