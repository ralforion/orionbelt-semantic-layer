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

MySQL saturated rather than refusing, which is why this is worth typing
correctly rather than leaving to the engine (#323). Its own casts now carry 38
digits so that column can no longer be reached (#336), but the other engines
still refuse, and a declared width is what makes the measure portable.
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
      WideIntAvg: {code: wia, abstractType: int, sqlPrecision: 38, sqlScale: 0}
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
  Wide Avg:
    columns: [{dataObject: Charges, column: WideIntAvg}]
    resultType: int
    aggregation: avg
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

    This is the residual: such a measure still overflows on the engines that
    refuse one. It no longer saturates on MySQL, whose casts carry 38 digits
    for that reason (#336), so the failure is at least loud everywhere it
    happens. Pinned so the limit is visible rather than assumed away.
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


# MySQL floors a measure's decimal cast at 38 digits of its own, because it
# saturates an overflow where the others raise (#336). The widening still has
# to reach it - a narrower resolved type would show up as a narrower cast -
# it just cannot land on the same number. Substrings rather than whole type
# names, since the engines spell a decimal differently and the point is width.
WIDENED_TO = dict.fromkeys(["postgres", "duckdb", "clickhouse", "snowflake"], "25, 2") | {
    "mysql": "38, 2"
}


@pytest.mark.parametrize("dialect", sorted(WIDENED_TO))
def test_the_widening_reaches_every_dialect(dialect: str) -> None:
    sql = _sql("Wide Sum", dialect)
    assert WIDENED_TO[dialect] in sql, sql


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


class TestAnIntegerAverageCombinesBothWidenings:
    """The declared width and the 64-bit room are different requirements.

    A source declared ``decimal(38, 0)`` averaged to ``decimal(21, 2)`` still
    overflows: 21 minus 2 is nineteen integer digits and the column holds
    thirty-eight. Measured on PostgreSQL with a value the column legally holds,
    ``CAST(AVG(amt) AS DECIMAL(21, 2))`` raises where ``DECIMAL(38, 0)``
    returns 99999999999999999999999999999999999998.
    """

    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "snowflake"])
    def test_an_exact_dialect_honours_the_declared_width(self, dialect: str) -> None:
        assert "38, 0" in _sql("Wide Avg", dialect), _sql("Wide Avg", dialect)

    def test_duckdb_honours_the_declared_width_too(self) -> None:
        """Widening carries a better number here now (#316).

        It used not to: DuckDB averages in DOUBLE, so a wider type carried a
        rounded value without complaining, and refusing to widen was what kept
        the failure loud. The average is assembled from integer arithmetic now,
        so the wider type holds an exact figure and this engine joins the three
        that honour the declaration.
        """
        sql = _sql("Wide Avg", "duckdb")
        assert "38, 0" in sql, sql
