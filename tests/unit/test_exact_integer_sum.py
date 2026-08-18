"""An integer ``SUM`` is exact on every dialect, including the two that wrap.

``SUM`` over a 64-bit column accumulates in 64 bits on ClickHouse and Dremio,
so it wraps rather than overflowing. Measured on two rows of
9000000000000000000, against ClickHouse 25 and Dremio OSS:

===========  ==============================
DuckDB       raises
PostgreSQL   raises
BigQuery     raises
Databricks   raises
Snowflake    ``18000000000000000000``
MySQL        ``9223372036854775807`` (#336)
ClickHouse   ``-446744073709551616``
Dremio       ``-446744073709551616``
===========  ==============================

A negative total from two positive rows is the one outcome no output type can
repair: ``sumWithOverflow`` returns the same value on ClickHouse, and so does
``CAST(SUM(x) AS Decimal(38, 0))``, because the accumulator has already wrapped
by the time anything casts it. Casting the **argument** returns
18000000000000000000 on both engines.

This is the same overflow ``exact_integer_avg`` already dodges *inside* its own
rewrite - ``dialect/base.py`` calls the inner cast "the load-bearing part" and
cites this exact value - reached by the plainer road. It was fixed for the
aggregate that drifts and not for the one that wraps (#338).

Values are checked against live engines in
``tests/integration/drift/vendor_exec/test_overflow_cast_exec.py``; what is
asserted here is which dialects rewrite, what type the rewrite carries, and
what it declines to touch.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: "1.0"
name: exact_integer_sum
dataObjects:
  Charges:
    code: charges
    columns:
      Qty: {code: qty, abstractType: int}
      Amt: {code: amt, abstractType: float}
      Day: {code: day, abstractType: date}
dimensions:
  Charge Month: {dataObject: Charges, column: Day, timeGrain: month}
measures:
  Qty Sum: {columns: [{dataObject: Charges, column: Qty}], resultType: int, aggregation: sum}
  Qty Count: {columns: [{dataObject: Charges, column: Qty}], resultType: int, aggregation: count}
  Amt Sum: {columns: [{dataObject: Charges, column: Amt}], resultType: float, aggregation: sum}
  Qty Sum Pinned:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: sum
    dataType: "decimal(30, 0)"
metrics:
  Qty Running:
    type: cumulative
    measure: Qty Sum
    timeDimension: Charge Month
    dataType: "decimal(38, 0)"
"""

# The two whose accumulator wraps. Every other engine either answers exactly or
# refuses, so a rewrite there would be noise on eight dialects to fix two.
WRAPS = ["clickhouse", "dremio"]
DIALECTS = sorted(DialectRegistry.available())
UNAFFECTED = [d for d in DIALECTS if d not in WRAPS]


def _sql(measure: str, dialect: str, dimensions: list[str] | None = None) -> str:
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    return (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=dimensions or [], measures=[measure])),
            model,
            dialect,
        )
        .sql
    )


class TestTheRewrite:
    def test_clickhouse_widens_the_argument(self) -> None:
        """``toDecimal128``, the spelling its own AVG rewrite already uses."""
        assert "SUM(toDecimal128(" in _sql("Qty Sum", "clickhouse")

    def test_dremio_widens_the_argument(self) -> None:
        assert 'SUM(CAST("Charges"."qty" AS DECIMAL(38, 0)))' in _sql("Qty Sum", "dremio")

    @pytest.mark.parametrize("dialect", WRAPS)
    def test_the_result_type_moves_with_it(self, dialect: str) -> None:
        """Otherwise the exact 128-bit total is cast straight back into the 64
        bits it was rewritten to escape. An integer SUM infers ``bigint``
        (#315), which is why the rewrite has to carry its own type.
        """
        sql = _sql("Qty Sum", dialect)
        assert "38, 0" in sql, sql
        assert "Int64" not in sql and "BIGINT" not in sql, sql

    @pytest.mark.parametrize("dialect", WRAPS)
    def test_a_declared_type_still_wins(self, dialect: str) -> None:
        """The resolution order promises a declaration beats an inference, and
        the expression is rewritten under it either way - the same split the
        AVG rewrite makes. The accumulator wraps whatever the output type is.
        """
        sql = _sql("Qty Sum Pinned", dialect)
        assert "30, 0" in sql, sql
        assert "SUM(toDecimal128(" in sql or "SUM(CAST(" in sql, sql


class TestWhatIsNotRewritten:
    @pytest.mark.parametrize("dialect", UNAFFECTED)
    def test_an_engine_that_answers_or_refuses_is_left_alone(self, dialect: str) -> None:
        sql = _sql("Qty Sum", dialect)
        assert "SUM(toDecimal128(" not in sql, sql
        assert "SUM(CAST(" not in sql, sql

    @pytest.mark.parametrize("dialect", WRAPS)
    def test_a_float_sum_is_untouched(self, dialect: str) -> None:
        """Only ``resultType: int`` reaches the 64-bit accumulator. A
        fractional sum accumulates in a wider type already, and rewriting it
        would change the scale the model asked for.
        """
        assert "toDecimal128" not in _sql("Amt Sum", dialect)
        assert "SUM(CAST(" not in _sql("Amt Sum", dialect)

    @pytest.mark.parametrize("dialect", WRAPS)
    def test_a_count_is_untouched(self, dialect: str) -> None:
        sql = _sql("Qty Count", dialect)
        assert "toDecimal128" not in sql and "COUNT(" in sql, sql

    @pytest.mark.parametrize("dialect", WRAPS)
    def test_a_windowed_sum_keeps_todays_behaviour(self, dialect: str) -> None:
        """A cumulative wrapper builds a ``WindowFunction``, not a call, so the
        rewrite declines rather than guessing at a shape it cannot verify. The
        inner measure inside the CTE is still rewritten, which is where the
        accumulation over rows actually happens.
        """
        sql = _sql("Qty Running", dialect, ["Charge Month"])
        assert "OVER (" in sql, sql
        assert sql.count("SUM(toDecimal128(") + sql.count("SUM(CAST(") == 1, sql
