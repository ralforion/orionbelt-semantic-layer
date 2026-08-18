"""A measure over a column the model declares as wide gets a type that fits.

The built-in default carries 16 integer digits. A model saying its column is
``decimal(38, 15)`` has said, in the only vocabulary OBML has, that the column
holds values with up to 23 - so casting a total of them to the default cannot
work. Measured on a sum of 100000000000000001.10:

===========  ==========================  =========================
engine       cast to decimal(18, 2)      cast to the declared width
===========  ==========================  =========================
DuckDB       Conversion Error            100000000000000001.10
PostgreSQL   numeric field overflow      100000000000000001.10
MySQL        **9999999999999999.99**     100000000000000001.10
===========  ==========================  =========================

MySQL saturates with no warning, which is why this is worth typing correctly
rather than leaving to the engine (#323).
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: "1.0"
name: declared_width
dataObjects:
  Charges:
    code: charges
    columns:
      Day: {code: day, abstractType: int}
      Wide: {code: amt, abstractType: float, sqlPrecision: 38, sqlScale: 15}
      Money: {code: m, abstractType: float, sqlPrecision: 18, sqlScale: 2}
      Undeclared: {code: u, abstractType: float}
      HalfDeclared: {code: h, abstractType: float, sqlPrecision: 38}
      WideInt: {code: wi, abstractType: float, sqlPrecision: 38, sqlScale: 0}
dimensions:
  Day: {dataObject: Charges, column: Day}
measures:
  Wide Sum: {columns: [{dataObject: Charges, column: Wide}], resultType: float, aggregation: sum}
  Money Sum: {columns: [{dataObject: Charges, column: Money}], resultType: float, aggregation: sum}
  Undeclared Sum:
    columns: [{dataObject: Charges, column: Undeclared}]
    resultType: float
    aggregation: sum
  Half Sum:
    columns: [{dataObject: Charges, column: HalfDeclared}]
    resultType: float
    aggregation: sum
  Int Sum: {columns: [{dataObject: Charges, column: WideInt}], resultType: float, aggregation: sum}
  Expr Sum:
    expression: "{[Charges].[Wide]} * 1"
    resultType: float
    aggregation: sum
  Expr Narrow:
    expression: "{[Charges].[Money]} * 1"
    resultType: float
    aggregation: sum
  Wide Pinned:
    columns: [{dataObject: Charges, column: Wide}]
    resultType: float
    aggregation: sum
    dataType: "decimal(18, 2)"
"""


def _sql(measure: str, dialect: str = "postgres") -> str:
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


def test_a_column_declared_wider_than_the_default_widens_it() -> None:
    """23 integer digits declared, so 23 plus the default's scale."""
    assert "DECIMAL(25, 2)" in _sql("Wide Sum")


def test_a_column_that_fits_is_left_alone() -> None:
    """No widening for ordinary money: the default already holds it."""
    assert "DECIMAL(18, 2)" in _sql("Money Sum")


def test_an_undeclared_column_cannot_be_widened() -> None:
    """Nothing in the model says how large its values are.

    This is the residual: such a measure still overflows, and still saturates
    on MySQL. Pinned so the limit is visible rather than assumed away.
    """
    assert "DECIMAL(18, 2)" in _sql("Undeclared Sum")


def test_half_a_declaration_is_not_enough() -> None:
    """``sqlPrecision`` alone says nothing about scale.

    #313 established this the hard way: assuming a missing scale is 0 turned
    ``sqlPrecision: 18`` over a six-decimal column into ``DECIMAL(18, 0)`` and
    rounded every row before the aggregate saw it.
    """
    assert "DECIMAL(18, 2)" in _sql("Half Sum")


def test_an_explicit_data_type_still_wins() -> None:
    """The resolution order promises a declaration beats an inference."""
    assert "DECIMAL(18, 2)" in _sql("Wide Pinned")


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "duckdb", "clickhouse", "snowflake"])
def test_the_widening_reaches_every_dialect(dialect: str) -> None:
    sql = _sql("Wide Sum", dialect)
    assert "25, 2" in sql, sql


def test_the_scale_gives_way_when_the_integer_part_cannot_fit() -> None:
    """A source declared ``decimal(38, 0)`` holds 38 integer digits.

    Keeping the default's two decimals would leave 36 and overflow on a value
    the column holds quite legally - measured, DuckDB refuses
    99999999999999999999999999999999999999 cast to ``DECIMAL(38, 2)`` and
    accepts it at ``DECIMAL(38, 0)``. Dropping fractional places the source
    never had is the smaller loss.
    """
    assert "DECIMAL(38, 0)" in _sql("Int Sum")


def test_an_expression_measure_is_covered_too() -> None:
    """Keying on ``len(columns) == 1`` skipped every expression measure.

    That exact cut has now hidden a bug three times - the CFL leg alignment
    made it twice (#305, #311). An expression measure aggregates a formula over
    the same physical columns and has the same reason to outgrow the default,
    so the widest column it *references* decides, not its declared ``columns``.
    """
    assert "DECIMAL(25, 2)" in _sql("Expr Sum")


def test_an_expression_over_a_narrow_column_is_left_alone() -> None:
    """Reading the expression must not widen what does not need widening."""
    assert "DECIMAL(18, 2)" in _sql("Expr Narrow")
