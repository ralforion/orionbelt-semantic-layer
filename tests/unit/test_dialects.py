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
    Expr,
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
from orionbelt.dialect.base import AmbiguousTableReferenceError
from orionbelt.dialect.bigquery import BigQueryDialect
from orionbelt.dialect.clickhouse import ClickHouseDialect
from orionbelt.dialect.databricks import DatabricksDialect
from orionbelt.dialect.dremio import DremioDialect
from orionbelt.dialect.duckdb import DuckDBDialect
from orionbelt.dialect.mysql import MySQLDialect
from orionbelt.dialect.postgres import PostgresDialect
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.dialect.snowflake import SnowflakeDialect
from orionbelt.models.semantic import TimeGrain, WeekStart
from orionbelt.models.types import parse_data_type

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
        assert 'FROM "orders"' in sql

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

    def test_integer_cast_of_a_numeric_aggregate_uses_accurate_cast(
        self, dialect: ClickHouseDialect
    ) -> None:
        """``CAST`` wraps or saturates an overflowing integer, ``accurateCast`` raises (#356).

        The truncation is what the plain ``CAST`` already did to a fractional
        input, so no non-overflowing answer changes; ``accurateCast`` rejects a
        value carrying a fraction without it.
        """
        agg = FunctionCall(name="SUM", args=[col("qty")])
        expr = dialect.cast_to_obml_type(agg, parse_data_type("integer"))
        assert dialect.compile_expr(expr) == (
            "accurateCast(trunc(SUM(\"qty\")), 'Nullable(Int32)')"
        )

        wide = dialect.cast_to_obml_type(agg, parse_data_type("bigint"))
        assert dialect.compile_expr(wide) == (
            "accurateCast(trunc(SUM(\"qty\")), 'Nullable(Int64)')"
        )

    @pytest.mark.parametrize("name", ["SUM", "COUNT", "AVG", "STDDEV", "CORR", "VAR_POP"])
    def test_numeric_aggregates_are_guarded(self, dialect: ClickHouseDialect, name: str) -> None:
        """The set is named as the *compiler* builds it, not as this dialect renders it.

        ``stddev`` reaches ``_compile_cast`` as ``STDDEV`` and only becomes
        ``stddevSamp`` further down, so a set spelled in ClickHouse's own names
        matches nothing. It did, once.
        """
        expr = FunctionCall(name=name, args=[col("qty")])
        sql = dialect.compile_expr(dialect.cast_to_obml_type(expr, parse_data_type("integer")))
        assert sql.startswith("accurateCast(trunc("), sql

    @pytest.mark.parametrize("name", ["MIN", "MAX", "ANY_VALUE", "MEDIAN", "MODE", "LISTAGG"])
    def test_type_preserving_aggregates_keep_the_plain_cast(
        self, dialect: ClickHouseDialect, name: str
    ) -> None:
        """These carry their argument's type, so a Bool, Date or String can arrive.

        ``trunc`` refuses a String and ``toString`` turns a Bool into ``'true'``
        and a Date into ``'2026-08-15'``, so guarding these reshapes casts this
        dialect answers today: measured, ``MAX(flag)`` reads 1, ``MAX(day)``
        reads 20680 and ``MAX(code)`` reads 42 for ``'42'``. The compiler models
        no types over expression bodies, so the aggregate is the only thing that
        can be read off the AST, and these say nothing.
        """
        expr = FunctionCall(name=name, args=[col("v")])
        sql = dialect.compile_expr(dialect.cast_to_obml_type(expr, parse_data_type("integer")))
        assert sql.startswith("CAST("), sql
        assert "accurateCast" not in sql

    def test_selecting_aggregate_is_guarded_when_its_column_is_numeric(
        self, dialect: ClickHouseDialect
    ) -> None:
        """``MAX`` is numeric exactly when its argument is, and the ref now says so.

        This is the residue #356 left: a MIN or MAX over a column wider than the
        declared target still wrapped, because nothing told the dialect the
        argument was a number. ``ColumnRef.abstract_type`` does.
        """
        for type_name in ("int", "float"):
            agg = FunctionCall(name="MAX", args=[ColumnRef(name="big", abstract_type=type_name)])
            sql = dialect.compile_expr(dialect.cast_to_obml_type(agg, parse_data_type("integer")))
            assert sql == "accurateCast(trunc(MAX(\"big\")), 'Nullable(Int32)')", type_name

    @pytest.mark.parametrize("type_name", ["string", "boolean", "date", "timestamp", "json"])
    def test_selecting_aggregate_is_not_guarded_for_a_non_numeric_column(
        self, dialect: ClickHouseDialect, type_name: str
    ) -> None:
        """Measured: these answer today and must keep answering.

        ``MAX(flag)`` reads 1, ``MAX(day)`` reads 20680, ``MAX(code)`` reads 42
        for ``'42'``. ``trunc`` refuses all three.
        """
        agg = FunctionCall(name="MAX", args=[ColumnRef(name="v", abstract_type=type_name)])
        sql = dialect.compile_expr(dialect.cast_to_obml_type(agg, parse_data_type("integer")))
        assert sql.startswith("CAST("), sql

    def test_selecting_aggregate_over_an_untyped_ref_is_not_guarded(
        self, dialect: ClickHouseDialect
    ) -> None:
        """A ref invented for a CTE alias carries no type, and unknown means leave it.

        Only ``resolution._build_column_expr`` records a type. Wrappers that
        rewrite refs to point at a CTE genuinely do not know one, so those keep
        the behaviour this dialect has always had rather than guessing.
        """
        agg = FunctionCall(name="MAX", args=[ColumnRef(name="alias", table="cte")])
        sql = dialect.compile_expr(dialect.cast_to_obml_type(agg, parse_data_type("integer")))
        assert sql == 'CAST(MAX("cte"."alias") AS Nullable(Int32))'

    def test_integer_cast_of_a_bare_column_keeps_the_plain_cast(
        self, dialect: ClickHouseDialect
    ) -> None:
        """Nothing on a bare column says it is a number, so it is left alone."""
        expr = dialect.cast_to_obml_type(col("qty"), parse_data_type("integer"))
        assert dialect.compile_expr(expr) == 'CAST("qty" AS Nullable(Int32))'

    def test_wrapped_numeric_aggregates_stay_guarded(self, dialect: ClickHouseDialect) -> None:
        """The planner wraps a measure before it is cast, and each wrapper hid the aggregate.

        ``measure.defaultValue`` emits ``COALESCE(SUM(x), 0)``, which is how a
        guarded SUM stopped being guarded once: matching only the outermost node
        saw ``COALESCE`` and fell back to the unguarded cast. Arithmetic from a
        derived metric and a window from ``total: true`` are the same shape.
        """
        agg = FunctionCall(name="SUM", args=[col("qty")])
        target = parse_data_type("integer")
        wrapped: list[Expr] = [
            FunctionCall(name="COALESCE", args=[agg, Literal.number(0)]),
            BinaryOp(left=agg, op="*", right=Literal.number(2)),
            WindowFunction(func_name="SUM", args=[col("qty")]),
            CaseExpr(when_clauses=[(col("f"), agg)], else_clause=Literal.number(0)),
        ]
        for expr in wrapped:
            sql = dialect.compile_expr(dialect.cast_to_obml_type(expr, target))
            assert sql.startswith("accurateCast(trunc("), f"{type(expr).__name__}: {sql}"

    @pytest.mark.parametrize("name", ["RANK", "DENSE_RANK", "ROW_NUMBER", "NTILE"])
    def test_ranking_window_functions_are_guarded(
        self, dialect: ClickHouseDialect, name: str
    ) -> None:
        """A rank counts rows, so it is an integer whatever it is ordered over.

        ClickHouse types all four as ``UInt64``, and a ``UInt64`` past the
        target wraps exactly like a SUM does: measured,
        ``CAST(toUInt64(4000000000) AS Nullable(Int32))`` is **-294967296**
        while ``accurateCast(trunc(...))`` raises code 70. A window metric with
        ``dataType: integer`` reached the unguarded cast until this was added.
        """
        expr = WindowFunction(func_name=name, order_by=[OrderByItem(expr=col("qty"))])
        sql = dialect.compile_expr(dialect.cast_to_obml_type(expr, parse_data_type("integer")))
        assert sql.startswith("accurateCast(trunc("), sql

    def test_offsetting_window_functions_follow_their_first_argument(
        self, dialect: ClickHouseDialect
    ) -> None:
        """``LAG`` carries its argument's value, so it is numeric only when that is.

        Only the first argument is read: ``LAG(x, 1)`` carries an offset after
        it, and an offset says nothing about the type of the result. A reference
        into the window CTE carries no declared type, so that case keeps the
        plain ``CAST`` rather than guessing.
        """
        target = parse_data_type("integer")

        typed = WindowFunction(
            func_name="LAG",
            args=[ColumnRef(name="qty", abstract_type="int"), Literal.number(1)],
            order_by=[OrderByItem(expr=col("day"))],
        )
        assert dialect.compile_expr(dialect.cast_to_obml_type(typed, target)).startswith(
            "accurateCast(trunc("
        )

        for untyped in (
            WindowFunction(func_name="LAG", args=[col("Qty Sum"), Literal.number(1)]),
            WindowFunction(
                func_name="LAG",
                args=[ColumnRef(name="code", abstract_type="string"), Literal.number(1)],
            ),
        ):
            sql = dialect.compile_expr(dialect.cast_to_obml_type(untyped, target))
            assert sql.startswith("CAST("), sql

    def test_non_numeric_wrappers_are_not_guarded(self, dialect: ClickHouseDialect) -> None:
        """Unknown stays unknown: a wrapper over something unprovable is left alone."""
        target = parse_data_type("integer")
        for expr in (
            FunctionCall(name="COALESCE", args=[col("anything"), Literal.number(0)]),
            FunctionCall(name="SOME_VENDOR_FN", args=[col("qty")]),
            BinaryOp(left=col("a"), op="*", right=col("b")),
        ):
            sql = dialect.compile_expr(dialect.cast_to_obml_type(expr, target))
            assert sql.startswith("CAST("), sql

    def test_a_bare_numeric_literal_is_not_guarded(self, dialect: ClickHouseDialect) -> None:
        """It cannot overflow at run time, and every CFL count pad is one.

        It still counts as numeric *inside* the predicate, which is what lets
        ``COALESCE(SUM(x), 0)`` qualify.
        """
        expr = dialect.cast_to_obml_type(Literal.number(1), parse_data_type("integer"))
        assert dialect.compile_expr(expr) == "CAST(1 AS Nullable(Int32))"

    def test_integer_cast_of_null_literal_is_a_plain_cast(self, dialect: ClickHouseDialect) -> None:
        """A NULL pad carries a type and needs no accurate anything."""
        expr = dialect.cast_to_obml_type(Literal.null(), parse_data_type("integer"))
        assert dialect.compile_expr(expr) == "CAST(NULL AS Nullable(Int32))"

    def test_non_integer_casts_are_untouched(self, dialect: ClickHouseDialect) -> None:
        """The rewrite is scoped to integer targets; decimal keeps its pre-round.

        The pre-round runs over the value's own text because ``round`` answers a
        Float64 for a Float64 and ``CAST(Float64 AS Decimal)`` truncates, so
        rounding to the target scale and truncating at that same scale lost the
        place the round had just decided.
        """
        dec = dialect.cast_to_obml_type(col("amt"), parse_data_type("decimal(18, 2)"))
        assert dialect.compile_expr(dec) == (
            'CAST(round(toDecimal256(toString("amt"), 3), 2) AS Nullable(Decimal(18, 2)))'
        )
        text = dialect.cast_to_obml_type(col("amt"), parse_data_type("string"))
        assert dialect.compile_expr(text) == 'CAST("amt" AS Nullable(String))'


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
        """A week follows the model's calendar rather than the dialect, so the
        assertion is on the SQL rather than the node: ISO by default, and
        BigQuery's plain WEEK once the model says Sunday.
        """
        sql = dialect.compile_expr(dialect.render_time_grain(col("dt"), TimeGrain.WEEK))
        assert "ISOWEEK" in sql

        dialect.week_start = WeekStart.SUNDAY
        sunday = dialect.compile_expr(dialect.render_time_grain(col("dt"), TimeGrain.WEEK))
        assert "ISOWEEK" not in sunday
        assert "WEEK" in sunday

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
        assert ref == '"main"."orders"'

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
        assert dialect.capabilities.unsupported_aggregations == [
            "mode",
            "median",
            "corr",
            "covar_pop",
            "covar_samp",
            "regr_slope",
            "regr_intercept",
            "measure",
        ]

    def test_quote_identifier(self, dialect: MySQLDialect) -> None:
        assert dialect.quote_identifier("col") == "`col`"
        assert dialect.quote_identifier("has`tick") == "`has``tick`"

    def test_group_by_cube_raises_domain_error(self, dialect: MySQLDialect) -> None:
        """Regression: MySQL CUBE used to surface as bare NotImplementedError
        and become a 500 through the API. Must raise the structured
        ``UnsupportedGroupingError`` so routers map it to a 422 with
        dialect + grouping fields.
        """
        from orionbelt.dialect.base import UnsupportedGroupingError

        with pytest.raises(UnsupportedGroupingError) as exc:
            dialect.compile_group_by([ColumnRef(name="x")], "cube")
        assert exc.value.dialect == "mysql"
        assert exc.value.grouping == "cube"

    def test_group_by_rollup_unchanged(self, dialect: MySQLDialect) -> None:
        """ROLLUP path is unaffected by the CUBE error refactor."""
        sql = dialect.compile_group_by([ColumnRef(name="x")], "rollup")
        assert sql == "GROUP BY `x` WITH ROLLUP"

    def test_format_table_ref(self, dialect: MySQLDialect) -> None:
        ref = dialect.format_table_ref("ignored_db", "myschema", "orders")
        assert ref == "`myschema`.`orders`"

    def test_format_table_ref_escapes(self, dialect: MySQLDialect) -> None:
        ref = dialect.format_table_ref("db", "my`schema", "my`table")
        assert ref == "`my``schema`.`my``table`"

    def test_order_by_asc_no_nulls_position(self, dialect: MySQLDialect) -> None:
        """Plain ASC — MySQL default applies, no workaround."""
        item = OrderByItem(expr=ColumnRef(name="x"))
        assert dialect.compile_order_by(item) == "`x` ASC"

    def test_order_by_asc_nulls_first_matches_mysql_default(self, dialect: MySQLDialect) -> None:
        """ASC NULLS FIRST = MySQL default → emit plain ASC, no IS NULL hack."""
        item = OrderByItem(expr=ColumnRef(name="x"), nulls_last=False)
        assert dialect.compile_order_by(item) == "`x` ASC"

    def test_order_by_desc_nulls_last_matches_mysql_default(self, dialect: MySQLDialect) -> None:
        """DESC NULLS LAST = MySQL default → emit plain DESC, no workaround."""
        item = OrderByItem(expr=ColumnRef(name="x"), desc=True, nulls_last=True)
        assert dialect.compile_order_by(item) == "`x` DESC"

    def test_order_by_asc_nulls_last_uses_workaround(self, dialect: MySQLDialect) -> None:
        """ASC NULLS LAST conflicts with MySQL default → IS NULL workaround."""
        item = OrderByItem(expr=ColumnRef(name="x"), nulls_last=True)
        assert dialect.compile_order_by(item) == "`x` IS NULL ASC, `x` ASC"

    def test_order_by_desc_nulls_first_uses_workaround(self, dialect: MySQLDialect) -> None:
        """DESC NULLS FIRST conflicts with MySQL default → IS NULL workaround."""
        item = OrderByItem(expr=ColumnRef(name="x"), desc=True, nulls_last=False)
        assert dialect.compile_order_by(item) == "`x` IS NULL DESC, `x` DESC"

    def test_compile_simple_select(self, dialect: MySQLDialect) -> None:
        ast = QueryBuilder().select(Star()).from_("orders").build()
        sql = dialect.compile(ast)
        assert "SELECT *" in sql
        assert "FROM `orders`" in sql

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
        """Breaking: a weekly bucket is the week's start date, not a ``%Y-%u``
        year-week label.

        MySQL was the only dialect labelling the bucket instead of dating it,
        which made a weekly dimension incomparable with the same model's
        ``date_trunc('week', ...)`` and with every other engine.
        """
        sql = dialect.compile_expr(dialect.render_time_grain(col("dt"), TimeGrain.WEEK))
        assert "%Y-%u" not in sql
        assert "WEEKDAY(" in sql

        dialect.week_start = WeekStart.SUNDAY
        sunday = dialect.compile_expr(dialect.render_time_grain(col("dt"), TimeGrain.WEEK))
        assert "DAYOFWEEK(" in sunday

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

    def test_compile_median_raises(self, dialect: MySQLDialect) -> None:
        from orionbelt.dialect.base import UnsupportedAggregationError

        expr = FunctionCall(name="MEDIAN", args=[ColumnRef(name="price")])
        with pytest.raises(UnsupportedAggregationError, match="mysql.*MEDIAN"):
            dialect.compile_expr(expr)

    def test_compile_mode_raises(self, dialect: MySQLDialect) -> None:
        from orionbelt.dialect.base import UnsupportedAggregationError

        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        with pytest.raises(UnsupportedAggregationError, match="mysql.*MODE"):
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

    def test_cast_boolean_uses_signed_not_tinyint(self, dialect: MySQLDialect) -> None:
        """``TINYINT(1)`` is a legal column type and an illegal cast target (#357).

        This asserted ``TINYINT(1)`` before, which MySQL rejects with error 1064,
        so a measure declaring ``dataType: boolean`` validated clean and compiled
        to invalid SQL. ``SIGNED`` is MySQL's own reading of a boolean.
        """
        expr = Cast(expr=col("age"), type_name="boolean")
        assert dialect.compile_expr(expr) == "CAST(`age` AS SIGNED)"

    def test_cast_timestamp_uses_datetime(self, dialect: MySQLDialect) -> None:
        """``TIMESTAMP`` is not in MySQL's cast vocabulary either (#357).

        ``DATETIME`` is what this dialect's own ``_ABSTRACT_TYPE_MAP`` already
        uses; the typed-literal path was fixed for this and the measure
        ``dataType`` path was not.
        """
        expr = Cast(expr=col("seen"), type_name="timestamp")
        assert dialect.compile_expr(expr) == "CAST(`seen` AS DATETIME)"

    def test_cast_string_abstract_uses_safe_char_length(self, dialect: MySQLDialect) -> None:
        """Abstract ``string`` (VARCHAR(255)) maps to CHAR(255) — inside CHAR's limit."""
        expr = Cast(expr=col("name"), type_name="string")
        sql = dialect.compile_expr(expr)
        assert sql == "CAST(`name` AS CHAR(255))"

    def test_cast_obml_string_drops_oversized_length(self, dialect: MySQLDialect) -> None:
        """OBML ``string`` resolves to VARCHAR(65535) for DDL; in CAST that
        exceeds CHAR's 255-char column limit, so the dialect must drop the
        length and emit plain ``CHAR`` rather than the invalid ``CHAR(65535)``.
        """
        from orionbelt.models.types import SimpleType

        rendered = dialect.render_obml_type(SimpleType(name="string"))
        assert rendered == "VARCHAR(65535)"
        expr = Cast(expr=col("name"), type_name=rendered)
        sql = dialect.compile_expr(expr)
        assert sql == "CAST(`name` AS CHAR)"

    def test_cast_varchar_no_length(self, dialect: MySQLDialect) -> None:
        expr = Cast(expr=col("name"), type_name="VARCHAR")
        sql = dialect.compile_expr(expr)
        assert sql == "CAST(`name` AS CHAR)"

    def test_cast_varchar_small_length_preserved(self, dialect: MySQLDialect) -> None:
        expr = Cast(expr=col("name"), type_name="VARCHAR(64)")
        sql = dialect.compile_expr(expr)
        assert sql == "CAST(`name` AS CHAR(64))"


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
        ("dremio", "CURRENT_DATE", "TIMESTAMPADD"),
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
        """ORDER BY on different column than aggregated raises in ClickHouse/Databricks.

        A domain ``UnsupportedAggregationError`` subclass, not a bare
        ``ValueError``: routers translate the former to a 422 and would surface
        the latter as a 500.
        """
        from orionbelt.dialect.base import CrossColumnOrderNotSupportedError

        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(
            name="LISTAGG",
            args=[ColumnRef(name="product_name")],
            order_by=[OrderByItem(expr=ColumnRef(name="created_at"))],
            separator=",",
        )
        with pytest.raises(CrossColumnOrderNotSupportedError) as excinfo:
            dialect.compile_expr(expr)
        assert excinfo.value.dialect == dialect_name
        assert excinfo.value.aggregation == "listagg"

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
        from orionbelt.dialect.base import UnsupportedAggregationError

        dialect = DialectRegistry.get("dremio")
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        with pytest.raises(UnsupportedAggregationError, match="dremio.*MODE"):
            dialect.compile_expr(expr)

    def test_mysql_mode_raises(self) -> None:
        """MySQL does not support MODE."""
        from orionbelt.dialect.base import UnsupportedAggregationError

        dialect = DialectRegistry.get("mysql")
        expr = FunctionCall(name="MODE", args=[ColumnRef(name="status")])
        with pytest.raises(UnsupportedAggregationError, match="mysql.*MODE"):
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
            ("postgres", "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY"),
            ("snowflake", "MEDIAN("),
        ],
    )
    def test_median(self, dialect_name: str, expected: str) -> None:
        dialect = DialectRegistry.get(dialect_name)
        expr = FunctionCall(name="MEDIAN", args=[ColumnRef(name="price")])
        sql = dialect.compile_expr(expr)
        assert expected in sql

    def test_median_mysql_unsupported(self) -> None:
        from orionbelt.dialect.base import UnsupportedAggregationError

        dialect = DialectRegistry.get("mysql")
        expr = FunctionCall(name="MEDIAN", args=[ColumnRef(name="price")])
        with pytest.raises(UnsupportedAggregationError, match="mysql.*MEDIAN"):
            dialect.compile_expr(expr)


class TestTableRefWithoutDatabase:
    """``database`` is optional in OBML, and an omitted one must be dropped.

    Quoting it anyway produced ``""."schema"."table"``, which Snowflake
    rejects with ``Database '""' does not exist`` and BigQuery/Databricks
    with the backquoted equivalent. Dropping it lets the reference resolve
    against the connection's current database, which is what lets one model
    serve several deployments of the same schema.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ],
    )
    def test_empty_database_is_omitted(self, name: str) -> None:
        ref = DialectRegistry.get(name).format_table_ref("", "orionbelt_1", "sales")
        assert "orionbelt_1" in ref and "sales" in ref
        # No empty quoted component of any quoting style.
        for empty in ('""', "``", "[]"):
            assert empty not in ref, f"{name}: {ref}"

    @pytest.mark.parametrize("name", ["bigquery", "databricks", "snowflake"])
    def test_three_part_ref_is_unchanged_when_database_is_set(self, name: str) -> None:
        dialect = DialectRegistry.get(name)
        ref = dialect.format_table_ref("proj", "ds", "tbl")
        assert ref.count(".") == 2
        for part in ("proj", "ds", "tbl"):
            assert part in ref

    @pytest.mark.parametrize("name", ["bigquery", "databricks", "snowflake"])
    def test_database_without_schema_is_refused(self, name: str) -> None:
        """``proj.tbl`` would be read as ``schema.table``, not ``database.table``.

        The resolver defaults both fields to ``""``, so a model declaring
        ``database: PROD`` and no ``schema`` is legal OBML. Dropping the empty
        middle would silently point the query at a different namespace, and
        there is no portable three-part form with an empty middle, so this is
        refused rather than guessed.
        """
        with pytest.raises(AmbiguousTableReferenceError) as excinfo:
            DialectRegistry.get(name).format_table_ref("proj", "", "tbl")
        assert "proj" in str(excinfo.value)
        assert "schema" in str(excinfo.value)

    @pytest.mark.parametrize("name", ["clickhouse", "duckdb", "mysql", "postgres"])
    def test_two_part_dialects_collapse_an_empty_schema(self, name: str) -> None:
        """``database`` is not part of the name here, so this is not ambiguous."""
        ref = DialectRegistry.get(name).format_table_ref("proj", "", "tbl")
        assert "tbl" in ref
        assert "proj" not in ref
        for empty in ('""', "``"):
            assert empty not in ref

    def test_dremio_treats_it_as_a_two_level_path(self) -> None:
        """Dremio paths have no fixed arity, so the pair names what it says."""
        ref = DialectRegistry.get("dremio").format_table_ref("space", "", "tbl")
        assert ref == '"space"."tbl"'
