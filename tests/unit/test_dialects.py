"""Tests for SQL dialect system."""

from __future__ import annotations

import pytest

from orionbelt.ast.builder import QueryBuilder, col, eq, lit
from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    FunctionCall,
    InList,
    IsNull,
    Literal,
    OrderByItem,
    RelativeDateRange,
    Select,
    Star,
    WindowFunction,
)
from orionbelt.dialect import DialectRegistry
from orionbelt.dialect.bigquery import BigQueryDialect
from orionbelt.dialect.clickhouse import ClickHouseDialect
from orionbelt.dialect.databricks import DatabricksDialect
from orionbelt.dialect.dremio import DremioDialect
from orionbelt.dialect.duckdb import DuckDBDialect
from orionbelt.dialect.mysql import MySQLDialect
from orionbelt.dialect.postgres import PostgresDialect
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.dialect.snowflake import SnowflakeDialect
from orionbelt.models.semantic import TimeGrain

ALL_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]


class TestDialectRegistry:
    def test_available_dialects(self) -> None:
        available = DialectRegistry.available()
        for name in ALL_DIALECTS:
            assert name in available

    def test_get_postgres(self) -> None:
        dialect = DialectRegistry.get("postgres")
        assert isinstance(dialect, PostgresDialect)

    def test_get_snowflake(self) -> None:
        dialect = DialectRegistry.get("snowflake")
        assert isinstance(dialect, SnowflakeDialect)

    def test_unsupported_dialect_error(self) -> None:
        with pytest.raises(UnsupportedDialectError) as exc_info:
            DialectRegistry.get("oracle")
        assert "oracle" in str(exc_info.value)
        assert "postgres" in str(exc_info.value)


class TestPostgresDialect:
    @pytest.fixture
    def dialect(self) -> PostgresDialect:
        return PostgresDialect()

    def test_name(self, dialect: PostgresDialect) -> None:
        assert dialect.name == "postgres"

    def test_capabilities(self, dialect: PostgresDialect) -> None:
        assert dialect.capabilities.supports_cte is True
        assert dialect.capabilities.supports_qualify is False
        assert dialect.capabilities.supports_ilike is True

    def test_quote_identifier(self, dialect: PostgresDialect) -> None:
        assert dialect.quote_identifier("name") == '"name"'
        assert dialect.quote_identifier('has"quote') == '"has""quote"'

    def test_compile_simple_select(self, dialect: PostgresDialect) -> None:
        ast = QueryBuilder().select(Star()).from_("orders").build()
        sql = dialect.compile(ast)
        assert "SELECT *" in sql
        assert "FROM orders" in sql

    def test_compile_with_alias(self, dialect: PostgresDialect) -> None:
        ast = (
            QueryBuilder()
            .select(AliasedExpr(expr=col("name"), alias="customer_name"))
            .from_("customers", alias="c")
            .build()
        )
        sql = dialect.compile(ast)
        assert '"customer_name"' in sql
        assert '"c"' in sql

    def test_compile_aggregation(self, dialect: PostgresDialect) -> None:
        ast = (
            QueryBuilder()
            .select(
                col("country", "c"),
                AliasedExpr(
                    expr=FunctionCall(name="SUM", args=[col("amount", "o")]),
                    alias="total",
                ),
            )
            .from_("orders", alias="o")
            .join("customers", on=eq(col("customer_id", "o"), col("id", "c")), alias="c")
            .group_by(col("country", "c"))
            .order_by(col("total"), desc=True)
            .limit(100)
            .build()
        )
        sql = dialect.compile(ast)
        assert "SELECT" in sql
        assert "SUM" in sql
        assert "GROUP BY" in sql
        assert "ORDER BY" in sql
        assert "DESC" in sql
        assert "LIMIT 100" in sql
        assert "LEFT JOIN" in sql

    def test_compile_where(self, dialect: PostgresDialect) -> None:
        ast = (
            QueryBuilder()
            .select(Star())
            .from_("t")
            .where(BinaryOp(left=col("status"), op="=", right=lit("active")))
            .build()
        )
        sql = dialect.compile(ast)
        assert "WHERE" in sql
        assert "'active'" in sql

    def test_compile_in_list(self, dialect: PostgresDialect) -> None:
        expr = InList(
            expr=col("status"),
            values=[lit("a"), lit("b")],
        )
        sql = dialect.compile_expr(expr)
        assert "IN" in sql
        assert "'a'" in sql

    def test_compile_is_null(self, dialect: PostgresDialect) -> None:
        expr = IsNull(expr=col("deleted_at"))
        sql = dialect.compile_expr(expr)
        assert "IS NULL" in sql

    def test_compile_is_not_null(self, dialect: PostgresDialect) -> None:
        expr = IsNull(expr=col("email"), negated=True)
        sql = dialect.compile_expr(expr)
        assert "IS NOT NULL" in sql

    def test_compile_case(self, dialect: PostgresDialect) -> None:
        expr = CaseExpr(
            when_clauses=[(eq(col("status"), lit("active")), lit("Yes"))],
            else_clause=lit("No"),
        )
        sql = dialect.compile_expr(expr)
        assert "CASE" in sql
        assert "WHEN" in sql
        assert "THEN" in sql
        assert "ELSE" in sql
        assert "END" in sql

    def test_compile_cast(self, dialect: PostgresDialect) -> None:
        expr = Cast(expr=col("age"), type_name="INTEGER")
        sql = dialect.compile_expr(expr)
        assert "CAST" in sql
        assert "INTEGER" in sql

    def test_time_grain(self, dialect: PostgresDialect) -> None:
        result = dialect.render_time_grain(col("order_date"), TimeGrain.MONTH)
        assert isinstance(result, FunctionCall)
        assert result.name == "date_trunc"

    def test_compile_null_literal(self, dialect: PostgresDialect) -> None:
        assert dialect.compile_expr(Literal.null()) == "NULL"

    def test_compile_boolean_literals(self, dialect: PostgresDialect) -> None:
        assert dialect.compile_expr(Literal.boolean(True)) == "TRUE"
        assert dialect.compile_expr(Literal.boolean(False)) == "FALSE"

    def test_compile_distinct_function(self, dialect: PostgresDialect) -> None:
        f = FunctionCall(name="COUNT", args=[col("id")], distinct=True)
        sql = dialect.compile_expr(f)
        assert "DISTINCT" in sql


class TestSnowflakeDialect:
    @pytest.fixture
    def dialect(self) -> SnowflakeDialect:
        return SnowflakeDialect()

    def test_name(self, dialect: SnowflakeDialect) -> None:
        assert dialect.name == "snowflake"

    def test_capabilities(self, dialect: SnowflakeDialect) -> None:
        assert dialect.capabilities.supports_qualify is True
        assert dialect.capabilities.supports_time_travel is True

    def test_quote_identifier(self, dialect: SnowflakeDialect) -> None:
        assert dialect.quote_identifier("col") == '"col"'

    def test_time_grain(self, dialect: SnowflakeDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.MONTH)
        assert isinstance(result, FunctionCall)
        assert result.name == "DATE_TRUNC"

    def test_string_contains(self, dialect: SnowflakeDialect) -> None:
        result = dialect.render_string_contains(col("name"), lit("foo"))
        assert isinstance(result, FunctionCall)
        assert result.name == "CONTAINS"


class TestClickHouseDialect:
    @pytest.fixture
    def dialect(self) -> ClickHouseDialect:
        return ClickHouseDialect()

    def test_name(self, dialect: ClickHouseDialect) -> None:
        assert dialect.name == "clickhouse"

    def test_time_grain_month(self, dialect: ClickHouseDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.MONTH)
        assert isinstance(result, FunctionCall)
        assert result.name == "toStartOfMonth"

    def test_time_grain_year(self, dialect: ClickHouseDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.YEAR)
        assert isinstance(result, FunctionCall)
        assert result.name == "toStartOfYear"

    def test_cast_to_int(self, dialect: ClickHouseDialect) -> None:
        result = dialect.render_cast(col("val"), "INT")
        assert isinstance(result, FunctionCall)
        assert result.name == "toInt64"


class TestDatabricksDialect:
    @pytest.fixture
    def dialect(self) -> DatabricksDialect:
        return DatabricksDialect()

    def test_name(self, dialect: DatabricksDialect) -> None:
        assert dialect.name == "databricks"

    def test_backtick_quoting(self, dialect: DatabricksDialect) -> None:
        assert dialect.quote_identifier("col") == "`col`"
        assert dialect.quote_identifier("has`tick") == "`has``tick`"


class TestDremioDialect:
    @pytest.fixture
    def dialect(self) -> DremioDialect:
        return DremioDialect()

    def test_name(self, dialect: DremioDialect) -> None:
        assert dialect.name == "dremio"

    def test_capabilities(self, dialect: DremioDialect) -> None:
        assert dialect.capabilities.supports_arrays is False
        assert dialect.capabilities.supports_ilike is False


class TestBigQueryDialect:
    @pytest.fixture
    def dialect(self) -> BigQueryDialect:
        return BigQueryDialect()

    def test_name(self, dialect: BigQueryDialect) -> None:
        assert dialect.name == "bigquery"

    def test_capabilities(self, dialect: BigQueryDialect) -> None:
        assert dialect.capabilities.supports_cte is True
        assert dialect.capabilities.supports_qualify is True
        assert dialect.capabilities.supports_arrays is True
        assert dialect.capabilities.supports_semi_structured is True
        assert dialect.capabilities.supports_ilike is False

    def test_backtick_quoting(self, dialect: BigQueryDialect) -> None:
        assert dialect.quote_identifier("col") == "`col`"

    def test_time_grain(self, dialect: BigQueryDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.MONTH)
        assert isinstance(result, FunctionCall)
        assert result.name == "DATE_TRUNC"

    def test_time_grain_week(self, dialect: BigQueryDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.WEEK)
        assert isinstance(result, FunctionCall)
        assert result.name == "DATE_TRUNC"
        # BigQuery uses ISOWEEK for week truncation
        sql = dialect.compile_expr(result)
        assert "ISOWEEK" in sql

    def test_type_map(self, dialect: BigQueryDialect) -> None:
        assert dialect._resolve_type_name("string") == "STRING"
        assert dialect._resolve_type_name("int") == "INT64"
        assert dialect._resolve_type_name("float") == "FLOAT64"
        assert dialect._resolve_type_name("boolean") == "BOOL"
        assert dialect._resolve_type_name("json") == "JSON"

    def test_median(self, dialect: BigQueryDialect) -> None:
        expr = FunctionCall(name="MEDIAN", args=[ColumnRef(name="price")])
        sql = dialect.compile_expr(expr)
        assert "APPROX_QUANTILES" in sql

    def test_mode(self, dialect: BigQueryDialect) -> None:
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        sql = dialect.compile_expr(expr)
        assert "APPROX_TOP_COUNT" in sql


class TestDuckDBDialect:
    @pytest.fixture
    def dialect(self) -> DuckDBDialect:
        return DuckDBDialect()

    def test_name(self, dialect: DuckDBDialect) -> None:
        assert dialect.name == "duckdb"

    def test_capabilities(self, dialect: DuckDBDialect) -> None:
        assert dialect.capabilities.supports_cte is True
        assert dialect.capabilities.supports_qualify is True
        assert dialect.capabilities.supports_arrays is True
        assert dialect.capabilities.supports_ilike is True

    def test_quote_identifier(self, dialect: DuckDBDialect) -> None:
        assert dialect.quote_identifier("col") == '"col"'
        assert dialect.quote_identifier('has"quote') == '"has""quote"'

    def test_time_grain(self, dialect: DuckDBDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.MONTH)
        assert isinstance(result, FunctionCall)
        assert result.name == "date_trunc"

    def test_two_part_table_ref(self, dialect: DuckDBDialect) -> None:
        ref = dialect.format_table_ref("db", "main", "orders")
        assert ref == "main.orders"

    def test_string_contains_ilike(self, dialect: DuckDBDialect) -> None:
        result = dialect.render_string_contains(col("name"), lit("foo"))
        sql = dialect.compile_expr(result)
        assert "ILIKE" in sql


class TestMySQLDialect:
    @pytest.fixture
    def dialect(self) -> MySQLDialect:
        return MySQLDialect()

    def test_name(self, dialect: MySQLDialect) -> None:
        assert dialect.name == "mysql"

    def test_capabilities(self, dialect: MySQLDialect) -> None:
        assert dialect.capabilities.supports_cte is True
        assert dialect.capabilities.supports_qualify is False
        assert dialect.capabilities.supports_ilike is False
        assert dialect.capabilities.supports_arrays is False
        assert dialect.capabilities.supports_union_all_by_name is False

    def test_quote_identifier(self, dialect: MySQLDialect) -> None:
        assert dialect.quote_identifier("col") == "`col`"
        assert dialect.quote_identifier("has`tick") == "`has``tick`"

    def test_format_table_ref(self, dialect: MySQLDialect) -> None:
        ref = dialect.format_table_ref("ignored_db", "myschema", "orders")
        assert ref == "`myschema`.`orders`"

    def test_format_table_ref_escapes(self, dialect: MySQLDialect) -> None:
        ref = dialect.format_table_ref("db", "my`schema", "my`table")
        assert ref == "`my``schema`.`my``table`"

    def test_compile_simple_select(self, dialect: MySQLDialect) -> None:
        ast = QueryBuilder().select(Star()).from_("orders").build()
        sql = dialect.compile(ast)
        assert "SELECT *" in sql
        assert "FROM orders" in sql

    def test_compile_aggregation(self, dialect: MySQLDialect) -> None:
        ast = (
            QueryBuilder()
            .select(
                col("country", "c"),
                AliasedExpr(
                    expr=FunctionCall(name="SUM", args=[col("amount", "o")]),
                    alias="total",
                ),
            )
            .from_("orders", alias="o")
            .join("customers", on=eq(col("customer_id", "o"), col("id", "c")), alias="c")
            .group_by(col("country", "c"))
            .order_by(col("total"), desc=True)
            .limit(100)
            .build()
        )
        sql = dialect.compile(ast)
        assert "SELECT" in sql
        assert "SUM" in sql
        assert "GROUP BY" in sql
        assert "ORDER BY" in sql
        assert "DESC" in sql
        assert "LIMIT 100" in sql
        # MySQL uses backtick quoting
        assert "`total`" in sql
        assert "`c`" in sql

    def test_time_grain_day(self, dialect: MySQLDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.DAY)
        assert isinstance(result, FunctionCall)
        assert result.name == "DATE_FORMAT"

    def test_time_grain_month(self, dialect: MySQLDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.MONTH)
        assert isinstance(result, FunctionCall)
        assert result.name == "DATE_FORMAT"
        sql = dialect.compile_expr(result)
        assert "%Y-%m-01" in sql

    def test_time_grain_quarter(self, dialect: MySQLDialect) -> None:
        from orionbelt.ast.nodes import RawSQL

        result = dialect.render_time_grain(col("dt"), TimeGrain.QUARTER)
        assert isinstance(result, RawSQL)
        assert "MAKEDATE" in result.sql
        assert "QUARTER" in result.sql

    def test_time_grain_year(self, dialect: MySQLDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.YEAR)
        assert isinstance(result, FunctionCall)
        sql = dialect.compile_expr(result)
        assert "%Y-01-01" in sql

    def test_time_grain_week(self, dialect: MySQLDialect) -> None:
        result = dialect.render_time_grain(col("dt"), TimeGrain.WEEK)
        assert isinstance(result, FunctionCall)
        sql = dialect.compile_expr(result)
        assert "%Y-%u" in sql

    def test_compile_listagg(self, dialect: MySQLDialect) -> None:
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="val")],
            separator=",",
        )
        sql = dialect.compile_expr(expr)
        assert "GROUP_CONCAT(" in sql
        assert "SEPARATOR ','" in sql

    def test_compile_listagg_with_order_by(self, dialect: MySQLDialect) -> None:
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="val")],
            order_by=[OrderByItem(expr=ColumnRef(name="val"))],
            separator="; ",
        )
        sql = dialect.compile_expr(expr)
        assert "GROUP_CONCAT(" in sql
        assert "ORDER BY" in sql
        assert "SEPARATOR '; '" in sql

    def test_compile_median(self, dialect: MySQLDialect) -> None:
        expr = FunctionCall(name="MEDIAN", args=[ColumnRef(name="price")])
        sql = dialect.compile_expr(expr)
        assert "PERCENTILE_CONT(0.5)" in sql
        assert "ORDER BY" in sql

    def test_compile_mode_raises(self, dialect: MySQLDialect) -> None:
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        with pytest.raises(ValueError, match="MySQL does not support MODE"):
            dialect.compile_expr(expr)

    def test_current_date_sql(self, dialect: MySQLDialect) -> None:
        assert dialect.current_date_sql() == "CURDATE()"

    def test_date_add_positive(self, dialect: MySQLDialect) -> None:
        sql = dialect.date_add_sql("CURDATE()", "day", 7)
        assert sql == "DATE_ADD(CURDATE(), INTERVAL 7 DAY)"

    def test_date_add_negative(self, dialect: MySQLDialect) -> None:
        sql = dialect.date_add_sql("CURDATE()", "day", -7)
        assert sql == "DATE_SUB(CURDATE(), INTERVAL 7 DAY)"

    def test_type_map(self, dialect: MySQLDialect) -> None:
        assert dialect._resolve_type_name("string") == "VARCHAR(255)"
        assert dialect._resolve_type_name("boolean") == "TINYINT(1)"
        assert dialect._resolve_type_name("timestamp_tz") == "DATETIME"
        assert dialect._resolve_type_name("int") == "INT"
        assert dialect._resolve_type_name("float") == "DOUBLE"

    def test_string_contains_uses_concat(self, dialect: MySQLDialect) -> None:
        result = dialect.render_string_contains(col("name"), lit("foo"))
        sql = dialect.compile_expr(result)
        assert "LIKE" in sql
        assert "CONCAT(" in sql
        # Must NOT use || (logical OR in MySQL)
        assert "||" not in sql

    def test_multi_field_count_uses_concat(self, dialect: MySQLDialect) -> None:
        sql = dialect._compile_multi_field_count(
            [ColumnRef(name="a"), ColumnRef(name="b")], distinct=True
        )
        assert "CONCAT(" in sql
        assert "COUNT(DISTINCT" in sql
        assert "CHAR" in sql

    def test_registry_includes_mysql(self) -> None:
        assert "mysql" in DialectRegistry.available()

    def test_cast(self, dialect: MySQLDialect) -> None:
        expr = Cast(expr=col("age"), type_name="boolean")
        sql = dialect.compile_expr(expr)
        assert "CAST" in sql
        assert "TINYINT(1)" in sql


class TestCrossDialectConsistency:
    """Ensure the same query produces valid SQL across all dialects."""

    def _build_test_query(self) -> Select:
        return (
            QueryBuilder()
            .select(
                col("country"),
                AliasedExpr(
                    expr=FunctionCall(name="SUM", args=[col("amount")]),
                    alias="total",
                ),
            )
            .from_("orders")
            .where(BinaryOp(left=col("status"), op="=", right=lit("active")))
            .group_by(col("country"))
            .order_by(col("total"), desc=True)
            .limit(10)
            .build()
        )

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_all_dialects_produce_valid_sql(self, dialect_name: str) -> None:
        ast = self._build_test_query()
        dialect = DialectRegistry.get(dialect_name)
        sql = dialect.compile(ast)
        # All dialects should produce SELECT, FROM, WHERE, GROUP BY, ORDER BY, LIMIT
        assert "SELECT" in sql
        assert "FROM" in sql
        assert "WHERE" in sql
        assert "GROUP BY" in sql
        assert "ORDER BY" in sql
        assert "LIMIT" in sql
        assert "SUM" in sql


class TestWindowFunctionRendering:
    """Test window function rendering across all dialects."""

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_sum_over_empty(self, dialect_name: str) -> None:
        """SUM(x) OVER () — grand total."""
        dialect = DialectRegistry.get(dialect_name)
        wf = WindowFunction(func_name="SUM", args=[ColumnRef(name="amount")])
        sql = dialect.compile_expr(wf)
        assert "SUM(" in sql
        assert "OVER ()" in sql

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_count_distinct_over_empty(self, dialect_name: str) -> None:
        """COUNT(DISTINCT x) OVER ()."""
        dialect = DialectRegistry.get(dialect_name)
        wf = WindowFunction(
            func_name="COUNT",
            args=[ColumnRef(name="id")],
            distinct=True,
        )
        sql = dialect.compile_expr(wf)
        assert "COUNT(DISTINCT" in sql
        assert "OVER ()" in sql

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_with_partition_by(self, dialect_name: str) -> None:
        """SUM(x) OVER (PARTITION BY dept)."""
        dialect = DialectRegistry.get(dialect_name)
        wf = WindowFunction(
            func_name="SUM",
            args=[ColumnRef(name="amount")],
            partition_by=[ColumnRef(name="dept")],
        )
        sql = dialect.compile_expr(wf)
        assert "SUM(" in sql
        assert "PARTITION BY" in sql
        assert "OVER (" in sql

    def test_with_order_by(self) -> None:
        """ROW_NUMBER() OVER (ORDER BY salary DESC)."""
        dialect = DialectRegistry.get("postgres")
        wf = WindowFunction(
            func_name="ROW_NUMBER",
            args=[],
            order_by=[OrderByItem(expr=ColumnRef(name="salary"), desc=True)],
        )
        sql = dialect.compile_expr(wf)
        assert "ROW_NUMBER()" in sql
        assert "ORDER BY" in sql
        assert "DESC" in sql

    def test_with_partition_and_order(self) -> None:
        """SUM(x) OVER (PARTITION BY dept ORDER BY hire_date ASC)."""
        dialect = DialectRegistry.get("postgres")
        wf = WindowFunction(
            func_name="SUM",
            args=[ColumnRef(name="salary")],
            partition_by=[ColumnRef(name="dept")],
            order_by=[OrderByItem(expr=ColumnRef(name="hire_date"))],
        )
        sql = dialect.compile_expr(wf)
        assert "PARTITION BY" in sql
        assert "ORDER BY" in sql


@pytest.mark.parametrize(
    ("dialect_name", "expected_date_fn", "expected_add_fn"),
    [
        ("bigquery", "CURRENT_DATE()", "DATE_ADD"),
        ("clickhouse", "today()", "addDays"),
        ("databricks", "current_date()", "date_add("),
        ("dremio", "CURRENT_DATE", "DATE_ADD"),
        ("duckdb", "CURRENT_DATE", "INTERVAL"),
        ("mysql", "CURDATE()", "DATE_SUB"),
        ("postgres", "CURRENT_DATE", "INTERVAL"),
        ("snowflake", "CURRENT_DATE()", "DATEADD('day'"),
    ],
)
def test_relative_date_range_compiles(
    dialect_name: str, expected_date_fn: str, expected_add_fn: str
) -> None:
    dialect = DialectRegistry.get(dialect_name)
    expr = RelativeDateRange(
        column=ColumnRef(name="order_date"),
        unit="day",
        count=7,
        direction="past",
        include_current=True,
    )
    sql = dialect.compile_expr(expr)
    assert "order_date" in sql
    assert expected_date_fn in sql
    assert expected_add_fn in sql


class TestListaggRendering:
    """Test LISTAGG rendering across all dialects."""

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "STRING_AGG"),
            ("clickhouse", "arrayStringConcat(groupArray("),
            ("databricks", "ARRAY_JOIN(COLLECT_LIST("),
            ("dremio", "LISTAGG"),
            ("duckdb", "STRING_AGG"),
            ("mysql", "GROUP_CONCAT"),
            ("postgres", "STRING_AGG"),
            ("snowflake", "LISTAGG"),
        ],
    )
    def test_basic_listagg(self, dialect_name: str, expected: str) -> None:
        """LISTAGG without DISTINCT or ORDER BY."""
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="product_name")],
            separator=",",
        )
        sql = dialect.compile_expr(expr)
        assert expected in sql
        assert "','" in sql

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "STRING_AGG(DISTINCT"),
            ("clickhouse", "groupUniqArray("),
            ("databricks", "ARRAY_JOIN(COLLECT_SET("),
            ("dremio", "LISTAGG(DISTINCT"),
            ("duckdb", "STRING_AGG(DISTINCT"),
            ("mysql", "GROUP_CONCAT(DISTINCT"),
            ("postgres", "STRING_AGG(DISTINCT"),
            ("snowflake", "LISTAGG(DISTINCT"),
        ],
    )
    def test_listagg_distinct(self, dialect_name: str, expected: str) -> None:
        """LISTAGG with DISTINCT."""
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="product_name")],
            distinct=True,
            separator=",",
        )
        sql = dialect.compile_expr(expr)
        assert expected in sql

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "ORDER BY"),
            ("clickhouse", "arraySort(groupArray("),
            ("databricks", "SORT_ARRAY(COLLECT_LIST("),
            ("dremio", "WITHIN GROUP (ORDER BY"),
            ("duckdb", "ORDER BY"),
            ("mysql", "ORDER BY"),
            ("postgres", "ORDER BY"),
            ("snowflake", "WITHIN GROUP (ORDER BY"),
        ],
    )
    def test_listagg_order_by(self, dialect_name: str, expected: str) -> None:
        """LISTAGG with ORDER BY."""
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="product_name")],
            order_by=[OrderByItem(expr=ColumnRef(name="product_name"))],
            separator="; ",
        )
        sql = dialect.compile_expr(expr)
        assert expected in sql
        assert "'; '" in sql

    @pytest.mark.parametrize(
        ("dialect_name", "expected_distinct", "expected_order"),
        [
            ("bigquery", "STRING_AGG(DISTINCT", "ORDER BY"),
            ("clickhouse", "arrayReverseSort(groupUniqArray(", ""),
            ("databricks", "SORT_ARRAY(COLLECT_SET(", ""),
            ("dremio", "LISTAGG(DISTINCT", "WITHIN GROUP (ORDER BY"),
            ("duckdb", "STRING_AGG(DISTINCT", "ORDER BY"),
            ("mysql", "GROUP_CONCAT(DISTINCT", "ORDER BY"),
            ("postgres", "STRING_AGG(DISTINCT", "ORDER BY"),
            ("snowflake", "LISTAGG(DISTINCT", "WITHIN GROUP (ORDER BY"),
        ],
    )
    def test_listagg_distinct_order_by(
        self, dialect_name: str, expected_distinct: str, expected_order: str
    ) -> None:
        """LISTAGG with DISTINCT + ORDER BY."""
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="product_name")],
            distinct=True,
            order_by=[OrderByItem(expr=ColumnRef(name="product_name"), desc=True)],
            separator=",",
        )
        sql = dialect.compile_expr(expr)
        assert expected_distinct in sql
        if expected_order:
            assert expected_order in sql

    def test_default_separator(self) -> None:
        """When separator is None, default comma is used."""
        dialect = DialectRegistry.get("postgres")
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="val")],
        )
        sql = dialect.compile_expr(expr)
        assert "STRING_AGG" in sql
        assert "','" in sql

    @pytest.mark.parametrize("dialect_name", ["clickhouse", "databricks"])
    def test_cross_column_order_by_raises(self, dialect_name: str) -> None:
        """ORDER BY on different column than aggregated raises in ClickHouse/Databricks."""
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="product_name")],
            order_by=[OrderByItem(expr=ColumnRef(name="created_at"))],
            separator=",",
        )
        with pytest.raises(ValueError, match="does not support ORDER BY on a different column"):
            dialect.compile_expr(expr)

    def test_clickhouse_desc_uses_reverse_sort(self) -> None:
        """ClickHouse uses arrayReverseSort for DESC ordering."""
        dialect = DialectRegistry.get("clickhouse")
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="val")],
            order_by=[OrderByItem(expr=ColumnRef(name="val"), desc=True)],
            separator=",",
        )
        sql = dialect.compile_expr(expr)
        assert "arrayReverseSort(" in sql

    def test_databricks_desc_uses_sort_array_false(self) -> None:
        """Databricks uses SORT_ARRAY(arr, false) for DESC ordering."""
        dialect = DialectRegistry.get("databricks")
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="val")],
            order_by=[OrderByItem(expr=ColumnRef(name="val"), desc=True)],
            separator=",",
        )
        sql = dialect.compile_expr(expr)
        assert "SORT_ARRAY(" in sql
        assert "false)" in sql


class TestAnyValueRendering:
    """Test ANY_VALUE rendering across all dialects."""

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "ANY_VALUE("),
            ("clickhouse", "any("),
            ("databricks", "ANY_VALUE("),
            ("dremio", "ANY_VALUE("),
            ("duckdb", "ANY_VALUE("),
            ("mysql", "ANY_VALUE("),
            ("postgres", "ANY_VALUE("),
            ("snowflake", "ANY_VALUE("),
        ],
    )
    def test_any_value(self, dialect_name: str, expected: str) -> None:
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(name="ANY_VALUE", args=[ColumnRef(name="status")])
        sql = dialect.compile_expr(expr)
        assert expected in sql


class TestModeRendering:
    """Test MODE rendering across dialects."""

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "APPROX_TOP_COUNT("),
            ("clickhouse", "topK(1)("),
            ("databricks", "MODE("),
            ("duckdb", "MODE("),
            ("postgres", "MODE() WITHIN GROUP (ORDER BY"),
            ("snowflake", "MODE("),
        ],
    )
    def test_mode(self, dialect_name: str, expected: str) -> None:
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        sql = dialect.compile_expr(expr)
        assert expected in sql

    def test_dremio_mode_raises(self) -> None:
        """Dremio does not support MODE."""
        dialect = DialectRegistry.get("dremio")
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        with pytest.raises(ValueError, match="Dremio does not support MODE"):
            dialect.compile_expr(expr)

    def test_mysql_mode_raises(self) -> None:
        """MySQL does not support MODE."""
        dialect = DialectRegistry.get("mysql")
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        with pytest.raises(ValueError, match="MySQL does not support MODE"):
            dialect.compile_expr(expr)


class TestMedianRendering:
    """Test MEDIAN rendering across all dialects."""

    @pytest.mark.parametrize(
        ("dialect_name", "expected"),
        [
            ("bigquery", "APPROX_QUANTILES("),
            ("clickhouse", "MEDIAN("),
            ("databricks", "MEDIAN("),
            ("dremio", "MEDIAN("),
            ("duckdb", "MEDIAN("),
            ("mysql", "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY"),
            ("postgres", "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY"),
            ("snowflake", "MEDIAN("),
        ],
    )
    def test_median(self, dialect_name: str, expected: str) -> None:
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(name="MEDIAN", args=[ColumnRef(name="price")])
        sql = dialect.compile_expr(expr)
        assert expected in sql
