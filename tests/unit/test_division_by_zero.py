"""A zero divisor yields NULL, on every dialect.

Left alone, the same ratio means five different things across the supported
engines. Measured on ``SUM(amt) / SUM(qty)`` with a zero divisor:

===========  ===========================
DuckDB       ``inf``
MySQL        NULL
PostgreSQL   raises ``division by zero``
BigQuery     raises ``400 division by zero``
ClickHouse   raises code 153
===========  ===========================

A semantic layer cannot promise that a measure means one thing everywhere and
then hand back a number, a null and an error depending on the warehouse behind
it. NULL is the answer chosen in #319: it reads as "no value" in a BI tool, it
is what MySQL already did, and it removes the ``inf`` - the only one of the
three outcomes that can silently corrupt a downstream figure rather than
stopping.

Values are checked against live engines in the vendor suites; what is asserted
here is that the guard is emitted, where, and where it is deliberately not.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: "1.0"
name: divzero
dataObjects:
  S:
    code: s
    columns:
      Day: {code: day, abstractType: int}
      Amt: {code: amt, abstractType: float}
      Qty: {code: qty, abstractType: int}
      Sold On: {code: sold_on, abstractType: date}
dimensions:
  Day: {dataObject: S, column: Day}
  Sale Month: {dataObject: S, column: Sold On, timeGrain: month}
measures:
  Amount: {columns: [{dataObject: S, column: Amt}], resultType: float, aggregation: sum}
  Orders: {columns: [{dataObject: S, column: Qty}], resultType: int, aggregation: sum}
  Rate Expr:
    expression: "{[S].[Amt]} / {[S].[Qty]}"
    resultType: float
    aggregation: sum
  Halved:
    expression: "{[S].[Amt]} / 2"
    resultType: float
    aggregation: sum
  Amount Total:
    columns: [{dataObject: S, column: Amt}]
    resultType: float
    aggregation: avg
    total: true
metrics:
  AOV: {expression: "{[Amount]} / {[Orders]}"}
"""

DIALECTS = sorted(DialectRegistry.available())


def _sql(measure: str, dialect: str) -> str:
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    return (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=["Day"], measures=[measure])), model, dialect
        )
        .sql
    )


class TestTheGuardIsEmittedEverywhereADivisionIs:
    """Guarded where divisions are *compiled*, so no site has to remember."""

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_metric_expression_is_guarded(self, dialect: str) -> None:
        assert "NULLIF(" in _sql("AOV", dialect), _sql("AOV", dialect)

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_measure_expression_is_guarded(self, dialect: str) -> None:
        assert "NULLIF(" in _sql("Rate Expr", dialect), _sql("Rate Expr", dialect)

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_generated_division_is_guarded_too(self, dialect: str) -> None:
        """``total: true`` on an AVG builds its own SUM/COUNT division.

        The point of guarding at the compile step rather than at each
        construction site: a division OBSL generates itself is exposed to a
        zero divisor exactly as a modeller's is, and would otherwise have
        needed its own guard and its own test to remember it.
        """
        assert "NULLIF(" in _sql("Amount Total", dialect), _sql("Amount Total", dialect)


class TestWhatIsNotGuarded:
    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_non_zero_literal_divisor_is_left_alone(self, dialect: str) -> None:
        """There is nothing to guard, and the noise would reach every snapshot."""
        sql = _sql("Halved", dialect)
        assert "NULLIF(" not in sql, sql

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_query_without_division_is_untouched(self, dialect: str) -> None:
        sql = _sql("Amount", dialect)
        assert "NULLIF(" not in sql, sql


def test_the_guard_survives_nesting() -> None:
    """``a / (b / c)`` guards both divisors, and the guard does the grouping.

    ``NULLIF`` brings its own delimiters, so the inner division does not need
    parens of its own to keep binding correctly.
    """
    from orionbelt.ast.nodes import BinaryOp, ColumnRef

    pg = DialectRegistry.get("postgres")
    col = lambda name: ColumnRef(name=name, table="t")  # noqa: E731
    ast = BinaryOp(left=col("a"), op="/", right=BinaryOp(left=col("b"), op="/", right=col("c")))
    assert pg.compile_expr(ast) == '"t"."a" / NULLIF("t"."b" / NULLIF("t"."c", 0), 0)'


def test_period_over_period_does_not_acquire_a_second_guard() -> None:
    """PoP guarded its own divisor long before this existed.

    It now passes the divisor through unguarded and lets the central guard
    apply, rather than emitting ``NULLIF(NULLIF(x, 0), 0)``.
    """
    yaml = (
        MODEL_YAML
        + """  Growth:
    type: period_over_period
    expression: '{[Amount]}'
    periodOverPeriod:
      timeDimension: Sale Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: ratio
"""
    )
    raw, sm = TrackedLoader().load_string(yaml)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    sql = (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=["Sale Month"], measures=["Growth"])),
            model,
            "postgres",
        )
        .sql
    )
    assert "NULLIF(" in sql, sql
    assert "NULLIF(NULLIF(" not in sql, sql


class TestTheStringBuiltDivisionPath:
    """``render_decimal_division_sql`` - the path PoP uses.

    Divisions are built two ways: as ``BinaryOp`` nodes, and as raw SQL
    strings. Both need the guard, and the second is the one that got away.
    """

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_every_dialect_guards_it_including_the_overriding_ones(self, dialect: str) -> None:
        """The regression this class exists for.

        ClickHouse and MySQL override this method to widen their operands.
        When the guard moved out of ``pop_wrap`` and into the dialect layer,
        both overrides silently dropped it, and a period-over-period ratio
        against a zero previous value went from NULL back to ILLEGAL_DIVISION
        on ClickHouse - measured, code 153.

        The fix was structural rather than another two edits: the guard now
        lives in the method dialects do not override, and widening moved to
        ``_render_decimal_division`` underneath it. This test pins that a new
        dialect cannot reintroduce the hole by overriding the wrong one.
        """
        sql = DialectRegistry.get(dialect).render_decimal_division_sql("cur", "prev")
        assert "NULLIF(prev, 0)" in sql, f"{dialect} divides by an unguarded divisor: {sql}"

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_the_widening_dialects_still_widen(self, dialect: str) -> None:
        """Guarding must not have cost ClickHouse and MySQL their precision."""
        sql = DialectRegistry.get(dialect).render_decimal_division_sql("cur", "prev")
        if dialect in ("clickhouse", "mysql"):
            assert "38, 14" in sql, sql
