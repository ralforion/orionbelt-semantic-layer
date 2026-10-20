"""Tests for SQL AST nodes, visitor, and builder."""

from __future__ import annotations

from orionbelt.ast.builder import QueryBuilder, and_, col, eq, func, lit
from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    Cast,
    ColumnRef,
    From,
    FunctionCall,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    Select,
    Star,
    WindowFunction,
)
from orionbelt.ast.visitor import ASTVisitor
from orionbelt.dialect.clickhouse import ClickHouseDialect


class TestLiteral:
    def test_string_literal(self) -> None:
        val = Literal.string("hello")
        assert val.value == "hello"

    def test_number_literal(self) -> None:
        val = Literal.number(42)
        assert val.value == 42

    def test_null_literal(self) -> None:
        val = Literal.null()
        assert val.value is None

    def test_boolean_literal(self) -> None:
        val = Literal.boolean(True)
        assert val.value is True


class TestColumnRef:
    def test_simple_column(self) -> None:
        c = ColumnRef(name="id")
        assert c.name == "id"
        assert c.table is None

    def test_qualified_column(self) -> None:
        c = ColumnRef(name="id", table="orders")
        assert c.table == "orders"


class TestFunctionCall:
    def test_simple_function(self) -> None:
        f = FunctionCall(name="SUM", args=[ColumnRef(name="amount")])
        assert f.name == "SUM"
        assert len(f.args) == 1

    def test_distinct_function(self) -> None:
        f = FunctionCall(name="COUNT", args=[ColumnRef(name="id")], distinct=True)
        assert f.distinct is True


class TestBinaryOp:
    def test_comparison(self) -> None:
        b = BinaryOp(
            left=ColumnRef(name="age"),
            op=">",
            right=Literal.number(18),
        )
        assert b.op == ">"


class TestSelect:
    def test_basic_select(self) -> None:
        s = Select(
            columns=[Star()],
            from_=From(source="orders"),
        )
        assert len(s.columns) == 1
        assert s.from_ is not None

    def test_select_with_joins(self) -> None:
        s = Select(
            columns=[ColumnRef(name="country", table="c")],
            from_=From(source="orders", alias="o"),
            joins=[
                Join(
                    join_type=JoinType.LEFT,
                    source="customers",
                    alias="c",
                    on=BinaryOp(
                        left=ColumnRef(name="customer_id", table="o"),
                        op="=",
                        right=ColumnRef(name="id", table="c"),
                    ),
                )
            ],
        )
        assert len(s.joins) == 1
        assert s.joins[0].join_type == JoinType.LEFT

    def test_frozen_dataclass(self) -> None:
        s = Select(columns=[Star()])
        # Frozen dataclass should be immutable
        with __import__("pytest").raises(AttributeError):
            s.limit = 10  # type: ignore[misc]


class TestQueryBuilder:
    def test_simple_select(self) -> None:
        ast = QueryBuilder().select(Star()).from_("orders").build()
        assert len(ast.columns) == 1
        assert ast.from_ is not None
        assert ast.from_.source == "orders"

    def test_full_query(self) -> None:
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
            .where(BinaryOp(left=col("status", "o"), op="=", right=lit("active")))
            .group_by(col("country", "c"))
            .order_by(col("total"), desc=True)
            .limit(100)
            .build()
        )
        assert len(ast.columns) == 2
        assert len(ast.joins) == 1
        assert ast.where is not None
        assert len(ast.group_by) == 1
        assert ast.limit == 100

    def test_with_cte(self) -> None:
        cte_query = QueryBuilder().select(Star()).from_("raw_orders").build()
        ast = (
            QueryBuilder()
            .with_cte("filtered_orders", cte_query)
            .select(Star())
            .from_("filtered_orders")
            .build()
        )
        assert len(ast.ctes) == 1
        assert ast.ctes[0].name == "filtered_orders"

    def test_where_chaining_uses_and(self) -> None:
        ast = (
            QueryBuilder()
            .select(Star())
            .from_("t")
            .where(eq(col("a"), lit(1)))
            .where(eq(col("b"), lit(2)))
            .build()
        )
        assert isinstance(ast.where, BinaryOp)
        assert ast.where.op == "AND"


class TestConvenienceFunctions:
    def test_col(self) -> None:
        c = col("name", "users")
        assert isinstance(c, ColumnRef)
        assert c.name == "name"
        assert c.table == "users"

    def test_func(self) -> None:
        f = func("COUNT", col("id"), distinct=True)
        assert isinstance(f, FunctionCall)
        assert f.distinct is True

    def test_lit(self) -> None:
        assert lit(42).value == 42
        assert lit("hello").value == "hello"
        assert lit(None).value is None

    def test_eq(self) -> None:
        e = eq(col("a"), lit(1))
        assert isinstance(e, BinaryOp)
        assert e.op == "="

    def test_and(self) -> None:
        result = and_(eq(col("a"), lit(1)), eq(col("b"), lit(2)))
        assert isinstance(result, BinaryOp)
        assert result.op == "AND"

    def test_and_empty(self) -> None:
        result = and_()
        assert isinstance(result, Literal)
        assert result.value is True


class TestWindowFunction:
    def test_basic_creation(self) -> None:
        wf = WindowFunction(func_name="SUM", args=[ColumnRef(name="amount")])
        assert wf.func_name == "SUM"
        assert len(wf.args) == 1
        assert wf.partition_by == []
        assert wf.order_by == []
        assert wf.distinct is False

    def test_with_partition_and_order(self) -> None:
        wf = WindowFunction(
            func_name="ROW_NUMBER",
            args=[],
            partition_by=[ColumnRef(name="dept")],
            order_by=[OrderByItem(expr=ColumnRef(name="salary"), desc=True)],
        )
        assert len(wf.partition_by) == 1
        assert len(wf.order_by) == 1
        assert wf.order_by[0].desc is True

    def test_frozen(self) -> None:
        wf = WindowFunction(func_name="SUM", args=[ColumnRef(name="x")])
        with __import__("pytest").raises(AttributeError):
            wf.func_name = "COUNT"  # type: ignore[misc]

    def test_equality(self) -> None:
        wf1 = WindowFunction(func_name="SUM", args=[ColumnRef(name="x")])
        wf2 = WindowFunction(func_name="SUM", args=[ColumnRef(name="x")])
        assert wf1 == wf2

    def test_distinct_flag(self) -> None:
        wf = WindowFunction(
            func_name="COUNT",
            args=[ColumnRef(name="id")],
            distinct=True,
        )
        assert wf.distinct is True


class TestASTVisitor:
    def test_visitor_identity(self) -> None:
        """Default visitor should return equivalent AST."""
        original = Select(
            columns=[col("a"), col("b")],
            from_=From(source="t"),
            where=eq(col("x"), lit(1)),
        )
        visitor = ASTVisitor()
        result = visitor.visit(original)
        assert isinstance(result, Select)
        assert len(result.columns) == 2

    def test_visitor_windowfunction(self) -> None:
        """Visitor should traverse WindowFunction nodes."""
        wf = WindowFunction(
            func_name="SUM",
            args=[ColumnRef(name="amount")],
            partition_by=[ColumnRef(name="dept")],
        )
        visitor = ASTVisitor()
        result = visitor.visit(wf)
        assert isinstance(result, WindowFunction)
        assert result.func_name == "SUM"
        assert len(result.args) == 1
        assert len(result.partition_by) == 1

    def test_visitor_keeps_a_casts_flags(self) -> None:
        """A visited cast renders as before: an OBML ``timestamp`` stays
        ``DateTime64(3)`` on ClickHouse rather than the abstract ``DateTime``."""
        cast = Cast(expr=ColumnRef(name="t"), type_name="timestamp", obml_type=True)
        result = ASTVisitor().visit(cast)
        assert result == cast
        assert "DateTime64(3)" in ClickHouseDialect().compile_expr(result)
