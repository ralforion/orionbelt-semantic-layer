"""Precedence-aware SQL emitter (v2.7.4, issue #79).

Pre-v2.7.4 every ``BinaryOp`` / ``IsNull`` / ``Between`` / ``InList`` /
``UnaryOp`` wrapped itself in parens unconditionally. Result: deeply
nested unreadable SQL like

    ON (((a = b) AND (c = d)) AND ((((y*100)+m)) = (((y2*100)+m2))))

Now the emitter wraps only when the child's precedence is strictly
less than the parent's required level, and never at the root of a
clause (SELECT / ON / WHERE / HAVING / GROUP BY / ORDER BY item).
Non-associative operators (= / <> / < / etc., LIKE) wrap on both
sides at equal precedence — SQL forbids chained comparisons.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import (
    Between,
    BinaryOp,
    ColumnRef,
    InList,
    IsNull,
    Literal,
    UnaryOp,
)
from orionbelt.dialect.registry import DialectRegistry


@pytest.fixture
def pg():
    return DialectRegistry.get("postgres")


def _col(name, table="t"):
    return ColumnRef(name=name, table=table)


def _bop(left, op, right):
    return BinaryOp(left=left, op=op, right=right)


class TestRootClause:
    """At the root of a clause (parent_prec = 0) the outer wrap is gone."""

    def test_arithmetic_no_outer_wrap(self, pg):
        # year * 100 + month
        ast = _bop(_bop(_col("year"), "*", Literal(value=100)), "+", _col("month"))
        assert pg.compile_expr(ast) == '"t"."year" * 100 + "t"."month"'

    def test_is_null_no_outer_wrap(self, pg):
        assert pg.compile_expr(IsNull(expr=_col("x"))) == '"t"."x" IS NULL'

    def test_is_not_null_no_outer_wrap(self, pg):
        assert pg.compile_expr(IsNull(expr=_col("x"), negated=True)) == '"t"."x" IS NOT NULL'

    def test_in_list_no_outer_wrap(self, pg):
        ast = InList(expr=_col("x"), values=[Literal(value=1), Literal(value=2)])
        assert pg.compile_expr(ast) == '"t"."x" IN (1, 2)'

    def test_between_no_outer_wrap(self, pg):
        ast = Between(expr=_col("x"), low=Literal(value=0), high=Literal(value=10))
        assert pg.compile_expr(ast) == '"t"."x" BETWEEN 0 AND 10'

    def test_and_no_outer_wrap(self, pg):
        ast = _bop(
            _bop(_col("a"), "=", _col("b")),
            "AND",
            _bop(_col("c"), "=", _col("d")),
        )
        assert pg.compile_expr(ast) == '"t"."a" = "t"."b" AND "t"."c" = "t"."d"'


class TestPrecedenceWrapping:
    """Wraps appear only where SQL precedence actually requires them."""

    def test_or_wraps_inside_and_no(self, pg):
        # AND has higher precedence than OR — no wrap on either side of OR
        ast = _bop(
            _bop(_col("a"), "=", Literal(value=1)),
            "OR",
            _bop(_col("b"), "=", Literal(value=2)),
        )
        assert pg.compile_expr(ast) == '"t"."a" = 1 OR "t"."b" = 2'

    def test_and_wraps_inside_mult_no(self, pg):
        # OR-containing child of AND needs wrapping
        inner_or = _bop(
            _bop(_col("a"), "=", Literal(value=1)),
            "OR",
            _bop(_col("b"), "=", Literal(value=2)),
        )
        outer = _bop(inner_or, "AND", _bop(_col("c"), "=", Literal(value=3)))
        sql = pg.compile_expr(outer)
        assert sql == '("t"."a" = 1 OR "t"."b" = 2) AND "t"."c" = 3'

    def test_addition_inside_multiplication_wraps(self, pg):
        # (a + b) * c — addition is lower precedence, needs wrap
        ast = _bop(_bop(_col("a"), "+", _col("b")), "*", _col("c"))
        assert pg.compile_expr(ast) == '("t"."a" + "t"."b") * "t"."c"'

    def test_multiplication_inside_addition_no_wrap(self, pg):
        # a + b * c — multiplication binds tighter, no wrap
        ast = _bop(_col("a"), "+", _bop(_col("b"), "*", _col("c")))
        assert pg.compile_expr(ast) == '"t"."a" + "t"."b" * "t"."c"'

    def test_subtraction_right_associative_wrap(self, pg):
        # a - (b - c) — non-associative requires wrap on right
        ast = _bop(_col("a"), "-", _bop(_col("b"), "-", _col("c")))
        assert pg.compile_expr(ast) == '"t"."a" - ("t"."b" - "t"."c")'

    def test_subtraction_left_associative_no_wrap(self, pg):
        # (a - b) - c renders as a - b - c (left-associative)
        ast = _bop(_bop(_col("a"), "-", _col("b")), "-", _col("c"))
        assert pg.compile_expr(ast) == '"t"."a" - "t"."b" - "t"."c"'

    def test_division_right_wraps(self, pg):
        # a / (b / c) — non-associative
        ast = _bop(_col("a"), "/", _bop(_col("b"), "/", _col("c")))
        assert pg.compile_expr(ast) == '"t"."a" / ("t"."b" / "t"."c")'


class TestComparisonChaining:
    """SQL forbids chained comparisons — emitter must wrap at equal prec."""

    def test_eq_of_geq_wraps(self, pg):
        # (a >= 100) = FALSE — the >= must be wrapped or DuckDB / Postgres /
        # every engine throws a syntax error on the chain ``a >= 100 = FALSE``.
        ast = _bop(_bop(_col("rev"), ">=", Literal(value=100)), "=", Literal(value=False))
        sql = pg.compile_expr(ast)
        assert sql == '("t"."rev" >= 100) = FALSE'

    def test_eq_of_eq_wraps(self, pg):
        ast = _bop(_bop(_col("a"), "=", _col("b")), "=", Literal(value=True))
        sql = pg.compile_expr(ast)
        assert sql == '("t"."a" = "t"."b") = TRUE'


class TestUnaryAndNotNullPredicates:
    def test_not_or_wraps(self, pg):
        # NOT (a OR b) — OR has lower precedence than NOT, needs wrap
        ast = UnaryOp(op="NOT", operand=_bop(_col("a"), "OR", _col("b")))
        sql = pg.compile_expr(ast)
        assert sql == 'NOT ("t"."a" OR "t"."b")'

    def test_is_null_inside_and_no_wrap(self, pg):
        # IS NULL has comparison precedence, AND is lower — no wrap
        ast = _bop(IsNull(expr=_col("a")), "AND", IsNull(expr=_col("b")))
        sql = pg.compile_expr(ast)
        assert sql == '"t"."a" IS NULL AND "t"."b" IS NULL'

    def test_in_list_inside_and_no_wrap(self, pg):
        ast = _bop(
            InList(expr=_col("a"), values=[Literal(value=1)]),
            "AND",
            IsNull(expr=_col("b")),
        )
        sql = pg.compile_expr(ast)
        assert sql == '"t"."a" IN (1) AND "t"."b" IS NULL'


class TestAcrossDialects:
    """The precedence rules live in BaseDialect and apply to every dialect."""

    @pytest.mark.parametrize(
        "dialect_name",
        [
            "postgres",
            "mysql",
            "duckdb",
            "clickhouse",
            "snowflake",
            "bigquery",
            "databricks",
            "dremio",
        ],
    )
    def test_arith_clean_on_every_dialect(self, dialect_name):
        d = DialectRegistry.get(dialect_name)
        ast = _bop(_bop(_col("year"), "*", Literal(value=100)), "+", _col("month"))
        sql = d.compile_expr(ast)
        # No outer wrap, no inner wrap (since * binds tighter than +)
        assert not sql.startswith("(")
        assert "* 100 +" in sql or "* 100) +" not in sql
