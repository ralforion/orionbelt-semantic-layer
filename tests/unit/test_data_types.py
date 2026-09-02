"""Tests for OBML data type registry, resolver, and dialect rendering."""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.type_resolver import (
    measure_source_is_exact,
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

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "DATETIME"),
            # ClickHouse is the exception and cannot be fixed by naming a type:
            # every DateTime64 carries a zone, defaulting to the server's. The
            # wall clock is preserved, only the label is the server's; pinning
            # one instead shifts the value, because relabelling a stored
            # DateTime moves it (measured: 13:45 Berlin becomes 11:45 UTC).
            ("clickhouse", "DateTime64(3)"),
            ("databricks", "TIMESTAMP_NTZ"),
            ("dremio", "TIMESTAMP"),
            ("duckdb", "TIMESTAMP"),
            ("mysql", "TIMESTAMP"),
            ("postgres", "TIMESTAMP"),
            ("snowflake", "TIMESTAMP_NTZ"),
        ],
    )
    def test_timestamp_renders_the_naive_type(self, dialect_name: str, expected: str) -> None:
        """OBML's cast vocabulary has one timestamp, and it is the naive one.

        ``timestamp_tz`` is a column declaration with no cast target (see
        ``resolution._CASTABLE_TEMPORAL_TYPES``), so rendering ``timestamp`` as
        a zoned type invents a zone the model never declared. PostgreSQL then
        moved the value -- a dimension declaring ``resultType: timestamp`` over
        ``2026-08-15 13:45:00`` came back as ``11:45 UTC`` -- and Snowflake
        attached the session zone to it.
        """
        from orionbelt.dialect.registry import DialectRegistry

        rendered = DialectRegistry.get(dialect_name).render_obml_type(SimpleType("timestamp"))
        assert rendered == expected
        assert rendered not in {"TIMESTAMPTZ", "TIMESTAMP_TZ", "TIMESTAMP WITH TIME ZONE"}

    @pytest.mark.parametrize("dialect_name", ["bigquery", "databricks"])
    def test_timestamp_and_timestamp_tz_are_not_synonyms(self, dialect_name: str) -> None:
        """Two OBML types must not render as one SQL type.

        Both engines spell the instant ``TIMESTAMP`` and mapping the naive type
        to it as well left a model no way to say which one it meant.
        """
        from orionbelt.dialect.registry import DialectRegistry

        dialect = DialectRegistry.get(dialect_name)
        naive = dialect._resolve_type_name(DataType.TIMESTAMP.value)
        zoned = dialect._resolve_type_name(DataType.TIMESTAMP_TZ.value)
        assert naive != zoned
        assert zoned == "TIMESTAMP"


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


class TestMeasureSourceIsExact:
    """When a measure's operand cannot be a float, and so needs no text route.

    ClickHouse converts through the value's own text to round a float exactly,
    which is most of the length of its generated SQL. An operand the model
    declares with a width cannot be a float, so the plain round is already
    exact - but only a caller holding the model can say so, since the AST
    carries an abstract type and OBML has no ``decimal`` among those.
    """

    @staticmethod
    def _model(**column_kwargs: object) -> SemanticModel:
        from orionbelt.models.semantic import DataColumnRef, DataObject, DataObjectColumn

        column = DataObjectColumn(
            name="Amount", code="amount", abstract_type=DataType.FLOAT, **column_kwargs
        )
        model = SemanticModel(
            data_objects={
                "Sales": DataObject(
                    name="Sales",
                    code="sales",
                    database="db",
                    schema_name="public",
                    columns={"Amount": column},
                )
            }
        )
        model.measures["Total"] = Measure(
            name="Total",
            aggregation="sum",
            columns=[DataColumnRef(view="Sales", column="Amount")],
        )
        return model

    def _exact(self, model: SemanticModel, **measure_kwargs: object) -> bool:
        measure = model.measures["Total"].model_copy(update=measure_kwargs)
        return measure_source_is_exact(measure, model)

    def test_a_declared_width_is_exact(self) -> None:
        model = self._model(sql_precision=7, sql_scale=2)
        assert self._exact(model) is True

    def test_an_integer_column_is_exact_without_a_width(self) -> None:
        """An integer has no fraction to lose, so nothing needs recovering."""
        model = self._model()
        model.data_objects["Sales"].columns["Amount"].abstract_type = DataType.INT
        assert self._exact(model) is True

    def test_a_bare_float_declaration_is_not(self) -> None:
        """The default for a numeric column, and the case the text route is for."""
        assert self._exact(self._model()) is False

    def test_half_a_width_is_not(self) -> None:
        """``sqlPrecision`` alone says nothing about scale, per #313."""
        assert self._exact(self._model(sql_precision=7)) is False

    def test_avg_is_not_exact_whatever_its_operand(self) -> None:
        """Measured: ``AVG(Decimal(7,2))`` is ``Float64`` on ClickHouse."""
        model = self._model(sql_precision=7, sql_scale=2)
        assert self._exact(model, aggregation="avg") is False

    def test_an_expression_measure_is_not(self) -> None:
        """It may divide, or name a column this cannot see."""
        model = self._model(sql_precision=7, sql_scale=2)
        assert self._exact(model, expression="{[Sales].[Amount]} / 2") is False

    def test_a_computed_column_is_not(self) -> None:
        model = self._model(sql_precision=7, sql_scale=2)
        model.data_objects["Sales"].columns["Amount"].expression = "{Other} / 2"
        assert self._exact(model) is False

    def test_a_fractional_default_is_not(self) -> None:
        """Measured: ``coalesce(SUM(dec), 0.5)`` is a Variant carrying a Float64."""
        model = self._model(sql_precision=7, sql_scale=2)
        assert self._exact(model, default_value=0.5) is False
        assert self._exact(model, default_value=0) is True

    @pytest.mark.parametrize("default", ["0", "0.5", True])
    def test_a_default_that_is_not_a_written_number_is_not(self, default: object) -> None:
        """``defaultValue`` takes a string, and it is emitted as one.

        ``defaultValue: "0"`` compiles to ``COALESCE(SUM(x), '0')``, whose type
        the engine decides: the plain round would be asking it to round whatever
        that comes out as. Measured, ``coalesce(SUM(dec), '0.5')`` answers 0.
        """
        model = self._model(sql_precision=7, sql_scale=2)
        assert self._exact(model, default_value=default) is False


class TestClickHouseSkipsTheTextRouteForAnExactOperand:
    """The rendering the flag buys, on the one dialect that reads it."""

    @staticmethod
    def _sql(*, source_exact: bool) -> str:
        from orionbelt.ast.nodes import ColumnRef, FunctionCall
        from orionbelt.models.types import parse_data_type

        dialect = ClickHouseDialect()
        agg = FunctionCall(name="SUM", args=[ColumnRef(name="amt", table="s")])
        cast = dialect.cast_to_obml_type(
            agg, parse_data_type("decimal(18, 2)"), source_exact=source_exact
        )
        return dialect.compile_expr(cast)

    def test_an_exact_operand_rounds_in_place(self) -> None:
        assert self._sql(source_exact=True) == (
            'CAST(round(SUM("s"."amt"), 2) AS Nullable(Decimal(18, 2)))'
        )

    def test_anything_else_still_converts_through_text(self) -> None:
        assert self._sql(source_exact=False) == (
            'CAST(round(toDecimal256(toString(SUM("s"."amt")), 3), 2) AS Nullable(Decimal(18, 2)))'
        )

    def test_the_flag_is_off_by_default(self) -> None:
        """A caller that says nothing gets the safe rendering."""
        from orionbelt.ast.nodes import ColumnRef
        from orionbelt.models.types import parse_data_type

        dialect = ClickHouseDialect()
        cast = dialect.cast_to_obml_type(ColumnRef(name="amt"), parse_data_type("decimal(18, 2)"))
        assert "toString(" in dialect.compile_expr(cast)


class TestCastSourceExactIsAHintNotAnIdentity:
    """Two casts of the same value to the same type are the same cast.

    The planners match expressions to remap an ORDER BY onto its projection, so
    a flag that split those matches would drop the ordering rather than change
    a rendering. It also has to survive the generic walkers, or a rewritten
    expression silently reverts to the long form.
    """

    @staticmethod
    def _casts() -> tuple[object, object]:
        from orionbelt.ast.nodes import Cast, ColumnRef

        inner = ColumnRef(name="amt", table="s")
        return (
            Cast(expr=inner, type_name="Decimal(18, 2)"),
            Cast(expr=inner, type_name="Decimal(18, 2)", source_exact=True),
        )

    def test_it_does_not_decide_equality(self) -> None:
        plain, exact = self._casts()
        assert plain == exact
        assert hash(plain) == hash(exact)

    def test_a_rewrite_carries_it(self) -> None:
        """Through a rewrite that actually rebuilds the node, not past it.

        ``map_nodes`` stops where *fn* answers a replacement, so a pass that
        rewrote only the column under the cast is what exercises the rebuild.
        """
        from orionbelt.ast.nodes import ColumnRef
        from orionbelt.compiler.expr_rewrite import map_nodes

        _, exact = self._casts()
        rewritten = map_nodes(
            exact,
            lambda node: ColumnRef(name="other") if isinstance(node, ColumnRef) else None,
        )
        assert rewritten.source_exact is True  # type: ignore[union-attr]
        assert rewritten.expr.name == "other"  # type: ignore[union-attr]

    def test_a_visitor_carries_it(self) -> None:
        from orionbelt.ast.visitor import ASTVisitor

        _, exact = self._casts()
        assert ASTVisitor().visit(exact).source_exact is True
