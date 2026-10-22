"""A ``resultType: float`` sum or average is the engine's ``double``.

It was cast to the ``decimal(18, 2)`` default, so an average of 1, 1, 1 and
1.25 came back 1.06. ``decimal`` keeps the exact default, and so do a model's
``defaultNumericDataType``, a measure's ``dataType`` and a ``float`` measure
over fixed-point columns: a ``float`` column declaring ``sqlPrecision`` and
``sqlScale``, the way models described a money column before OBML had
``decimal``.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest

from orionbelt.api.query_cache import build_type_map
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import DataType, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.pgwire.catalog import _measure_sql_type
from orionbelt.service.result_schema import declared_arrow_types

MODEL_YAML = """
version: "1.0"
name: float_double
{settings}
dataObjects:
  Customers:
    code: customers
    columns:
      Customer Id: {{code: id, abstractType: string, primaryKey: true}}
  Orders:
    code: orders
    columns:
      Order Id: {{code: id, abstractType: string, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: string}}
      Weight: {{code: weight, abstractType: float}}
      Price: {{code: price, abstractType: float, sqlPrecision: 18, sqlScale: 2}}
      Big Price: {{code: big_price, abstractType: float, sqlPrecision: 38, sqlScale: 4}}
      Half Price: {{code: half_price, abstractType: float, sqlPrecision: 18}}
      Quantity: {{code: quantity, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Order Customer],
          columnsTo: [Customer Id]}}
  Returns:
    code: returns
    columns:
      Return Id: {{code: id, abstractType: string, primaryKey: true}}
      Return Customer: {{code: customer_id, abstractType: string}}
      Returned Weight: {{code: weight, abstractType: float}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Return Customer],
          columnsTo: [Customer Id]}}
dimensions:
  Customer: {{dataObject: Customers, column: Customer Id, resultType: string}}
measures:
  Mean Weight: {{columns: [{{dataObject: Orders, column: Weight}}], aggregation: avg,
                 resultType: float}}
  Total Weight: {{columns: [{{dataObject: Orders, column: Weight}}], aggregation: sum,
                  resultType: float}}
  Weight Per Unit:
    expression: "{{[Orders].[Weight]}} / {{[Orders].[Quantity]}}"
    aggregation: sum
    resultType: float
  Pinned Weight: {{columns: [{{dataObject: Orders, column: Weight}}], aggregation: sum,
                   resultType: float, dataType: "decimal(18, 4)"}}
  Exact Amount: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum,
                  resultType: decimal}}
  Revenue: {{columns: [{{dataObject: Orders, column: Price}}], aggregation: sum,
             resultType: float}}
  Big Revenue: {{columns: [{{dataObject: Orders, column: Big Price}}], aggregation: sum,
                 resultType: float}}
  Revenue Times Two:
    expression: "{{[Orders].[Price]}} * 2"
    aggregation: sum
    resultType: float
  Mixed:
    expression: "{{[Orders].[Price]}} + {{[Orders].[Weight]}}"
    aggregation: sum
    resultType: float
  Half Revenue: {{columns: [{{dataObject: Orders, column: Half Price}}], aggregation: sum,
                  resultType: float}}
  Float Units: {{columns: [{{dataObject: Orders, column: Quantity}}], aggregation: avg,
                 resultType: float, defaultValue: 0}}
  Exact Mean: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: avg,
                resultType: decimal}}
  Inferred Revenue: {{columns: [{{dataObject: Orders, column: Price}}], aggregation: sum}}
  Inferred Big Revenue: {{columns: [{{dataObject: Orders, column: Big Price}}],
                          aggregation: sum}}
  Float Unit Sum: {{columns: [{{dataObject: Orders, column: Quantity}}], aggregation: sum,
                    resultType: float}}
  Distinct Mean: {{columns: [{{dataObject: Orders, column: Quantity}}], aggregation: avg,
                   distinct: true, resultType: float}}
  Distinct Unit Sum: {{columns: [{{dataObject: Orders, column: Quantity}}], aggregation: sum,
                       distinct: true, resultType: float}}
  Distinct Weight Sum: {{columns: [{{dataObject: Orders, column: Weight}}], aggregation: sum,
                         distinct: true, resultType: float}}
  Returned: {{columns: [{{dataObject: Returns, column: Returned Weight}}], aggregation: sum,
              resultType: float}}
"""


def _model(settings: str = "") -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML.format(settings=settings))
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _sql(measures: list[str], dialect: str = "duckdb", settings: str = "") -> str:
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Customer"], "measures": measures}}
    )
    return CompilationPipeline().compile(query, _model(settings), dialect).sql


class TestAFloatIsADouble:
    @pytest.mark.parametrize("measure", ["Mean Weight", "Total Weight", "Weight Per Unit"])
    def test_cast_to_double(self, measure: str) -> None:
        sql = _sql([measure])
        assert "AS DOUBLE)" in sql, sql
        assert "DECIMAL" not in sql, sql

    def test_over_int_columns_alone_too(self) -> None:
        assert "AS DOUBLE)" in _sql(["Float Units"])

    def test_the_average_is_not_rounded(self) -> None:
        """The case reported: four orders weighing 1, 1, 1 and 1.25."""
        sql = _sql(["Mean Weight"])
        con = duckdb.connect()
        con.execute("CREATE TABLE customers (id VARCHAR)")
        con.execute("INSERT INTO customers VALUES ('c')")
        con.execute("CREATE TABLE orders (id VARCHAR, customer_id VARCHAR, weight DOUBLE)")
        con.execute(
            "INSERT INTO orders VALUES"
            " ('a', 'c', 1), ('b', 'c', 1), ('d', 'c', 1), ('e', 'c', 1.25)"
        )
        assert con.execute(sql).fetchall() == [("c", 1.0625)]

    def test_a_multi_fact_pad_is_a_double_too(self) -> None:
        sql = _sql(["Total Weight", "Returned"])
        assert "UNION ALL" in sql
        assert "DECIMAL" not in sql, sql

    def test_every_surface_advertises_a_float(self) -> None:
        model = _model()
        assert declared_arrow_types(model)["Mean Weight"] == pa.float64()
        assert _measure_sql_type(model.measures["Mean Weight"], model) == "DOUBLE"
        assert build_type_map(model)["Mean Weight"] == "number"


class TestSnowflakeAveragesADouble:
    """Snowflake's ``AVG`` of 1, 1 and 2 is the ``NUMBER(38, 6)`` 1.333333, so
    a cast of the result is too late; the argument is averaged as a double."""

    def test_the_argument_is_cast(self) -> None:
        assert 'AVG(CAST("Orders"."weight" AS FLOAT))' in _sql(["Mean Weight"], "snowflake")

    def test_under_a_default_value_too(self) -> None:
        sql = (
            CompilationPipeline()
            .compile(
                QueryObject.model_validate({"select": {"measures": ["Float Units"]}}),
                _model(),
                "snowflake",
            )
            .sql
        )
        assert 'AVG(CAST("Orders"."quantity" AS FLOAT))' in sql

    @pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery"])
    def test_not_where_avg_keeps_the_digits(self, dialect: str) -> None:
        assert "AVG(CAST(" not in _sql(["Mean Weight"], dialect)

    def test_not_for_an_exact_average(self) -> None:
        assert "AVG(CAST(" not in _sql(["Exact Mean"], "snowflake")


class TestAWrappingAccumulatorIsAvoided:
    """ClickHouse and Dremio sum and average 64-bit integers in 64 bits: two
    rows of 9000000000000000000 sum to -446744073709551616, and the double
    cast around the aggregate carried that along."""

    @pytest.mark.parametrize(
        ("dialect", "double"), [("clickhouse", "Nullable(Float64)"), ("dremio", "DOUBLE")]
    )
    @pytest.mark.parametrize(
        ("measure", "agg"), [("Float Unit Sum", "SUM"), ("Float Units", "AVG")]
    )
    def test_the_argument_is_a_double(
        self, dialect: str, double: str, measure: str, agg: str
    ) -> None:
        sql = _sql([measure], dialect)
        assert f'{agg}(CAST("Orders"."quantity" AS {double}))' in sql, sql

    @pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", "databricks"])
    def test_not_where_the_accumulator_holds_or_raises(self, dialect: str) -> None:
        assert "SUM(CAST(" not in _sql(["Float Unit Sum"], dialect)

    @pytest.mark.parametrize(
        ("dialect", "widened"),
        [
            ("clickhouse", 'DISTINCT toDecimal128("Orders"."quantity", 0)'),
            ("dremio", 'DISTINCT CAST("Orders"."quantity" AS DECIMAL(38, 0))'),
        ],
    )
    @pytest.mark.parametrize("measure", ["Distinct Unit Sum", "Distinct Mean"])
    def test_a_distinct_integer_is_widened_exactly(
        self, dialect: str, widened: str, measure: str
    ) -> None:
        """A double would merge two integers past 2**53 into one value; the
        128-bit widening keeps every integer distinct and does not wrap."""
        sql = _sql([measure], dialect)
        assert f"SUM({widened})" in sql, sql

    @pytest.mark.parametrize("dialect", ["clickhouse", "dremio"])
    def test_a_distinct_float_is_not_widened(self, dialect: str) -> None:
        """Its accumulator does not wrap, and a cast to an integer width would
        truncate it."""
        sql = _sql(["Distinct Weight Sum"], dialect)
        assert "SUM(DISTINCT " in sql and "DISTINCT CAST(" not in sql, sql
        assert "toDecimal128" not in sql, sql

    def test_a_sum_past_64_bits_adds_up(self) -> None:
        """Executed on DuckDB, whose accumulator holds: the rewrite changes
        nothing there, the vendor test covers the engines it is for."""
        sql = _sql(["Float Unit Sum"])
        con = duckdb.connect()
        con.execute("CREATE TABLE customers (id VARCHAR)")
        con.execute("INSERT INTO customers VALUES ('c')")
        con.execute("CREATE TABLE orders (id VARCHAR, customer_id VARCHAR, quantity BIGINT)")
        con.execute("INSERT INTO orders VALUES ('a', 'c', 9e18), ('b', 'c', 9e18)")
        assert con.execute(sql).fetchall() == [("c", 1.8e19)]


class TestADistinctAverageDeduplicatesFirst:
    """``AVG(DISTINCT CAST(x AS FLOAT))`` merges integers that round to the same
    double: 0, 2**53 and 2**53 + 1 averaged two values."""

    def test_snowflake_divides_an_exact_distinct_sum(self) -> None:
        sql = _sql(["Distinct Mean"], "snowflake")
        expected = (
            'CAST(SUM(DISTINCT "Orders"."quantity") AS FLOAT)'
            ' / NULLIF(COUNT(DISTINCT "Orders"."quantity"), 0)'
        )
        assert expected in sql, sql
        assert "DISTINCT CAST(" not in sql, sql

    @pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery"])
    def test_elsewhere_the_distinct_average_is_native(self, dialect: str) -> None:
        sql = _sql(["Distinct Mean"], dialect)
        assert "AVG(DISTINCT " in sql and "DISTINCT CAST(" not in sql, sql


class TestADeclarationKeepsItExact:
    def test_the_model_default(self) -> None:
        sql = _sql(["Mean Weight"], settings='settings: {defaultNumericDataType: "decimal(18, 4)"}')
        assert "AS DECIMAL(18, 4))" in sql

    def test_the_measure_data_type(self) -> None:
        assert "AS DECIMAL(18, 4))" in _sql(["Pinned Weight"])

    def test_a_decimal_measure(self) -> None:
        assert "AS DECIMAL(18, 2))" in _sql(["Exact Amount"])


class TestAFixedPointColumnKeepsItExact:
    @pytest.mark.parametrize("measure", ["Revenue", "Revenue Times Two"])
    def test_a_float_measure_over_a_declared_width(self, measure: str) -> None:
        sql = _sql([measure])
        assert "AS DECIMAL(18, 2))" in sql, sql
        assert "DOUBLE" not in sql, sql

    def test_the_declared_width_still_widens_it(self) -> None:
        assert "AS DECIMAL(36, 2))" in _sql(["Big Revenue"])

    def test_a_sum_of_cents_adds_up(self) -> None:
        sql = _sql(["Revenue"])
        con = duckdb.connect()
        con.execute("CREATE TABLE customers (id VARCHAR)")
        con.execute("INSERT INTO customers VALUES ('c')")
        con.execute("CREATE TABLE orders (id VARCHAR, customer_id VARCHAR, price DECIMAL(18, 2))")
        con.execute("INSERT INTO orders SELECT i, 'c', 0.10 FROM range(10) t(i)")
        assert con.execute(sql).fetchall() == [("c", Decimal("1.00"))]

    @pytest.mark.parametrize("measure", ["Mixed", "Half Revenue"])
    def test_an_undeclared_float_read_makes_it_a_double(self, measure: str) -> None:
        """Half a width says nothing about the scale, and a measure reading any
        float column without one may be summing doubles."""
        assert "AS DOUBLE)" in _sql([measure])

    def test_every_surface_advertises_the_decimal(self) -> None:
        model = _model()
        types = declared_arrow_types(model)
        assert types["Revenue"] == pa.decimal128(18, 2)
        assert types["Big Revenue"] == pa.decimal128(36, 2)
        assert _measure_sql_type(model.measures["Revenue"], model) == "DECIMAL(18, 2)"
        assert build_type_map(model)["Revenue"] == "decimal(18, 2)"

    def test_an_inferred_decimal_is_advertised_at_its_cast_width(self) -> None:
        """It was advertised as the generic ``decimal(38, 10)``, which cannot
        hold the 34 integer digits of the ``decimal(36, 2)`` it is cast to."""
        model = _model()
        assert "AS DECIMAL(36, 2))" in _sql(["Inferred Big Revenue"])
        assert declared_arrow_types(model)["Inferred Big Revenue"] == pa.decimal128(36, 2)
        measure = model.measures["Inferred Big Revenue"]
        assert _measure_sql_type(measure, model) == "DECIMAL(36, 2)"
        assert build_type_map(model)["Inferred Big Revenue"] == "decimal(36, 2)"

    def test_a_declared_decimal_too(self) -> None:
        model = _model()
        assert declared_arrow_types(model)["Exact Amount"] == pa.decimal128(18, 2)
        assert _measure_sql_type(model.measures["Exact Amount"], model) == "DECIMAL(18, 2)"

    def test_an_omitted_result_type_infers_decimal(self) -> None:
        model = _model()
        assert model.measures["Inferred Revenue"].result_type is DataType.DECIMAL
        assert "AS DECIMAL(18, 2))" in _sql(["Inferred Revenue"])


class TestAnOssieImportIsTypedByItsColumns:
    """The importer wrote ``resultType: float`` on every sum and average, and
    ``int`` on every ``min``, ``max`` and ``mode``, whatever they read."""

    def _model(self) -> SemanticModel:
        import yaml
        from osi_orionbelt import OSItoOBML

        def field(name: str, datatype: str) -> dict[str, object]:
            return {
                "name": name,
                "datatype": datatype,
                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
            }

        def metric(name: str, expression: str) -> dict[str, object]:
            return {
                "name": name,
                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]},
            }

        osi = {
            "version": "0.2.0.dev0",
            "name": "sales",
            "datasets": [
                {
                    "name": "Orders",
                    "source": "A.P.ORDERS",
                    "primary_key": ["id"],
                    "fields": [
                        field("id", "String"),
                        field("amount", "Decimal"),
                        field("region", "String"),
                    ],
                }
            ],
            "metrics": [
                metric("Revenue", "SUM(Orders.amount)"),
                metric("Top Region", "MODE(Orders.region)"),
            ],
        }
        obml = OSItoOBML(osi).convert()
        raw, source_map = TrackedLoader().load_string(yaml.safe_dump(obml))
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def test_a_sum_of_a_decimal_field_stays_exact(self) -> None:
        assert self._model().measures["Revenue"].result_type is DataType.DECIMAL

    def test_a_mode_of_text_is_text(self) -> None:
        assert self._model().measures["Top Region"].result_type is DataType.STRING
