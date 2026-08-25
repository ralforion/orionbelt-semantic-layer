"""Tests for OBML data type registry, resolver, and dialect rendering."""

from __future__ import annotations

from decimal import Decimal

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
    DataType,
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
        m = Measure(name="Revenue", aggregation="sum", data_type="decimal(38, 8)")
        result = resolve_measure_data_type(m, None)
        assert result == DecimalType(38, 8)

    def test_count_infers_bigint(self) -> None:
        m = Measure(name="Order Count", aggregation="count")
        result = resolve_measure_data_type(m, None)
        assert result == SimpleType("bigint")

    def test_count_distinct_infers_bigint(self) -> None:
        m = Measure(name="Unique Customers", aggregation="count_distinct")
        result = resolve_measure_data_type(m, None)
        assert result == SimpleType("bigint")

    def test_division_infers_decimal_18_6(self) -> None:
        m = Measure(
            name="Rate",
            aggregation="sum",
            expression="{[Orders].[Amount]} / {[Orders].[Count]}",
        )
        result = resolve_measure_data_type(m, None)
        assert result == DIVISION_DEFAULT

    def test_sum_uses_builtin_default(self) -> None:
        m = Measure(name="Revenue", aggregation="sum")
        result = resolve_measure_data_type(m, None)
        assert result == BUILTIN_DEFAULT

    def test_avg_uses_builtin_default(self) -> None:
        m = Measure(name="Average", aggregation="avg")
        result = resolve_measure_data_type(m, None)
        assert result == BUILTIN_DEFAULT

    def test_model_settings_override(self) -> None:
        m = Measure(name="Revenue", aggregation="sum")
        settings = ModelSettings(default_numeric_data_type="decimal(18, 4)")
        result = resolve_measure_data_type(m, settings)
        assert result == DecimalType(18, 4)

    def test_min_passthrough(self) -> None:
        m = Measure(name="Min Price", aggregation="min")
        result = resolve_measure_data_type(m, None)
        assert result is None

    def test_max_passthrough(self) -> None:
        m = Measure(name="Max Price", aggregation="max")
        result = resolve_measure_data_type(m, None)
        assert result is None

    def test_listagg_passthrough(self) -> None:
        m = Measure(name="Names", aggregation="listagg")
        result = resolve_measure_data_type(m, None)
        assert result is None


class TestResolveMetricDataType:
    def test_explicit_wins(self) -> None:
        m = Metric(
            name="Rate",
            expression="{[Revenue]} / {[Count]}",
            data_type="decimal(18, 4)",
        )
        result = resolve_metric_data_type(m, None)
        assert result == DecimalType(18, 4)

    def test_division_infers_decimal_18_6(self) -> None:
        m = Metric(name="Rate", expression="{[Revenue]} / {[Count]}")
        result = resolve_metric_data_type(m, None)
        assert result == DIVISION_DEFAULT

    def test_simple_expression_uses_default(self) -> None:
        m = Metric(name="Total", expression="{[Revenue]} + {[Tax]}")
        result = resolve_metric_data_type(m, None)
        assert result == BUILTIN_DEFAULT


class TestDialectRendering:
    def test_postgres_decimal(self) -> None:
        d = PostgresDialect()
        assert d.render_obml_type(DecimalType(18, 2)) == "DECIMAL(18, 2)"

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
        # BigQuery: parameterized types not allowed in CAST → bare NUMERIC,
        # spill to BIGNUMERIC above precision 38.
        assert d.render_obml_type(DecimalType(18, 2)) == "NUMERIC"
        assert d.render_obml_type(DecimalType(76, 10)) == "BIGNUMERIC"
        # Scale decides as much as precision does. NUMERIC is (38, 9), so a
        # request for more scale than that has to spill too: measured on
        # BigQuery, CAST(1.000000000004 AS NUMERIC) returns 1 while BIGNUMERIC
        # returns the value, so answering NUMERIC here dropped the digits
        # silently for every caller, not only the CFL leg alignment that
        # surfaced it.
        assert d.render_obml_type(DecimalType(38, 9)) == "NUMERIC"
        assert d.render_obml_type(DecimalType(38, 12)) == "BIGNUMERIC"
        assert d.render_obml_type(DecimalType(18, 12)) == "BIGNUMERIC"

    def test_precision_clamping(self) -> None:
        d = SnowflakeDialect()
        # Snowflake max is 38
        result = d.render_obml_type(DecimalType(50, 10))
        assert result == "NUMBER(38, 10)"


class TestModelValidation:
    def test_valid_data_type_on_measure(self) -> None:
        m = Measure(name="Rev", aggregation="sum", data_type="decimal(18, 2)")
        assert m.data_type == "decimal(18, 2)"

    def test_invalid_data_type_on_measure(self) -> None:
        with pytest.raises(ValueError):
            Measure(name="Rev", aggregation="sum", data_type="varchar(255)")

    def test_valid_data_type_on_metric(self) -> None:
        m = Metric(name="Rate", expression="{[A]} / {[B]}", data_type="decimal(18, 6)")
        assert m.data_type == "decimal(18, 6)"

    def test_invalid_data_type_on_metric(self) -> None:
        with pytest.raises(ValueError):
            Metric(name="Rate", expression="{[A]} / {[B]}", data_type="number(18, 2)")

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
        assert "DECIMAL(18, 2)" in result.sql

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
        assert "DECIMAL(18, 6)" in result.sql

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
        assert "DECIMAL(38, 8)" in result.sql

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
        assert "DECIMAL(18, 4)" in result.sql


class TestAnIntegerMeasureIsNotTheNumericDefault:
    """The numeric default cannot describe a 64-bit result.

    ``decimal(18, 2)`` carries 16 integer digits and a BIGINT needs 19, so the
    cast meant to describe the result overflowed on values the source column
    held quite legally. The engine computed the sum and then failed converting
    it - on the plain star path, not only under a multi-fact plan.
    """

    def test_sum_of_an_integer_measure_infers_bigint(self) -> None:
        m = Measure(name="Qty Sum", aggregation="sum", result_type=DataType.INT)
        assert resolve_measure_data_type(m, None) == SimpleType("bigint")

    def test_an_explicit_data_type_still_wins(self) -> None:
        m = Measure(
            name="Qty Sum",
            aggregation="sum",
            result_type=DataType.INT,
            data_type="decimal(18, 2)",
        )
        assert resolve_measure_data_type(m, None) == DecimalType(18, 2)

    def test_a_float_measure_is_untouched(self) -> None:
        m = Measure(name="Revenue", aggregation="sum", result_type=DataType.FLOAT)
        assert resolve_measure_data_type(m, None) == BUILTIN_DEFAULT

    def test_avg_deliberately_keeps_the_default(self) -> None:
        """AVG is not widened, and the reason is not an oversight.

        Widening its cast would clear the overflow without making the average
        right, because the loss happens in the aggregate before any cast. So
        AVG is fixed by rewriting the *expression* instead, on the engines that
        have an exact route - see ``test_exact_avg.py``. That rewrite carries
        its own widened type, which is why nothing is inferred here.

        DuckDB has no exact route: casting the input, casting both operands and
        rewriting as SUM/COUNT were each measured to come back DOUBLE, because
        every division there is float division. It keeps the default and keeps
        failing loudly, which is the honest outcome and the reason this
        assertion still holds (#316).
        """
        m = Measure(name="Qty Avg", aggregation="avg", result_type=DataType.INT)
        assert resolve_measure_data_type(m, None) == BUILTIN_DEFAULT

    def test_a_bigint_sum_survives_execution(self) -> None:
        """The end of the story, run rather than asserted on the SQL."""
        duckdb = pytest.importorskip("duckdb")
        model = _bigint_model()
        con = _bigint_table(duckdb)
        query = QueryObject(select=QuerySelect(dimensions=["Day"], measures=["Qty Sum"]))
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert Decimal(str(con.execute(sql).fetchall()[0][1])) == Decimal("2000000000000000003")
        con.close()

    def test_a_bigint_avg_is_exact_rather_than_loud(self) -> None:
        """What replaced the loud overflow (#316).

        DuckDB averages in DOUBLE whatever the input type, so the value
        reaching the cast used to be 1000000000000000000 rather than ...001.5,
        and overflowing the default was the honest outcome. The average is now
        assembled from integer arithmetic - exact where the engine's own
        ``AVG`` is not - so there is nothing left to fail loudly about.
        """
        duckdb = pytest.importorskip("duckdb")
        model = _bigint_model()
        con = _bigint_table(duckdb)
        query = QueryObject(select=QuerySelect(dimensions=["Day"], measures=["Qty Avg"]))
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert Decimal(str(con.execute(sql).fetchall()[0][1])) == Decimal("1000000000000000001.50")
        con.close()

    def test_a_data_type_is_no_longer_needed_and_no_longer_lies(self) -> None:
        """``dataType`` used to be the escape hatch that was not one.

        The loss was inside the aggregate, so widening the output cast let a
        plausible wrong number through instead of failing: this test used to
        assert the *inequality*, as "still lossy". Both halves are settled now -
        the default type holds the exact average, and declaring a wider one
        returns the same exact figure rather than a rounded one.
        """
        duckdb = pytest.importorskip("duckdb")
        yaml = _BIGINT_YAML.replace(
            "Qty Avg: {columns: [{dataObject: Charges, column: Qty}], "
            "resultType: int, aggregation: avg}",
            "Qty Avg: {columns: [{dataObject: Charges, column: Qty}], "
            'resultType: int, aggregation: avg, dataType: "decimal(21, 2)"}',
        )
        from orionbelt.parser import ReferenceResolver, TrackedLoader

        raw, sm = TrackedLoader().load_string(yaml)
        model, _ = ReferenceResolver().resolve(raw, sm)
        con = _bigint_table(duckdb)
        query = QueryObject(select=QuerySelect(dimensions=["Day"], measures=["Qty Avg"]))
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert "DECIMAL(21, 2)" in sql, sql
        value = Decimal(str(con.execute(sql).fetchall()[0][1]))
        con.close()
        assert value == Decimal("1000000000000000001.50")


_BIGINT_YAML = """
version: "1.0"
name: bigint_sum
dataObjects:
  Charges:
    code: charges
    columns:
      Day: {code: day, abstractType: int}
      Qty: {code: qty, abstractType: int}
dimensions:
  Day: {dataObject: Charges, column: Day}
measures:
  Qty Sum: {columns: [{dataObject: Charges, column: Qty}], resultType: int, aggregation: sum}
  Qty Avg: {columns: [{dataObject: Charges, column: Qty}], resultType: int, aggregation: avg}
"""


def _bigint_model() -> SemanticModel:
    from orionbelt.parser import ReferenceResolver, TrackedLoader

    raw, sm = TrackedLoader().load_string(_BIGINT_YAML)
    model, _ = ReferenceResolver().resolve(raw, sm)
    return model


def _bigint_table(duckdb):  # type: ignore[no-untyped-def]
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE charges (day INTEGER, qty BIGINT)")
    con.execute("INSERT INTO charges VALUES (1, 1000000000000000001), (1, 1000000000000000002)")
    return con


class TestBigQueryNumericSpill:
    """NUMERIC is (38, 9), so it carries 29 integer digits however spelled.

    BigQuery documents the rule as ``P <= S + 29``. Checking only ``precision >
    38 or scale > 9`` let ``decimal(38, 0)`` through as NUMERIC, which needs 38
    integer digits - measured, a 38-digit value returns "400 numeric out of
    range" as NUMERIC and comes back intact as BIGNUMERIC. ``decimal(38, 2)``
    was wrong the same way, at 36.
    """

    @pytest.mark.parametrize(
        ("precision", "scale", "expected"),
        [
            (18, 2, "NUMERIC"),
            (21, 2, "NUMERIC"),
            (25, 2, "NUMERIC"),
            (38, 9, "NUMERIC"),  # the boundary: 29 integer digits exactly
            (38, 2, "BIGNUMERIC"),  # 36 integer digits
            (38, 0, "BIGNUMERIC"),  # 38
            (38, 15, "BIGNUMERIC"),  # scale past 9
            (76, 10, "BIGNUMERIC"),
        ],
    )
    def test_the_spill_follows_bigquery_own_rule(
        self, precision: int, scale: int, expected: str
    ) -> None:
        assert BigQueryDialect().render_obml_type(DecimalType(precision, scale)) == expected

    def test_the_exact_avg_input_cast_uses_the_same_rule(self) -> None:
        """It had its own copy, and the copy kept the old rule.

        That emitted ``AVG(CAST(x AS NUMERIC))`` under a BIGNUMERIC result for
        a source declared as 38 integer digits - the inner cast failing before
        the outer one could help.
        """
        from orionbelt.ast.nodes import ColumnRef

        dia = BigQueryDialect()
        rendered = dia.compile_expr(
            dia.exact_integer_avg(ColumnRef(name="amt", table="t"), DecimalType(38, 0))
        )
        assert "BIGNUMERIC" in rendered, rendered
        assert "NUMERIC)" not in rendered.replace("BIGNUMERIC)", ""), rendered
