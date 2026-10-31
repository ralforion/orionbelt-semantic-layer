"""Exact statistics of integers and decimals on Dremio, Snowflake, PostgreSQL and MySQL.

Dremio's variance and covariance and Snowflake's covariance, correlation and
regression subtract sums of squares or products as doubles and cancel far from
zero: 0 for the variance of 10^9 + 0..9 on Dremio, 0 for a covariance of 18.5
there on Snowflake. PostgreSQL's covariance, correlation and regression and
MySQL's variance read a value past a double's 53 bits as a double: 0 for the
variance of 10^17 + 0..5. They are computed from exact moments instead
(``Dialect.exact_statistic``). These tests pin which measures are rewritten and
the shape of the SQL; the vendor suite (``test_exact_statistics_exec``) runs it
against the exact statistic.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

#: (measure, column, aggregation), each a plain statistic of one column.
STATISTICS = [
    ("Qty Variance", "Qty", "variance"),
    ("Qty Var Pop", "Qty", "var_pop"),
    ("Qty Stddev", "Qty", "stddev"),
    ("Qty Stddev Pop", "Qty", "stddev_pop"),
    ("Price Variance", "Price", "variance"),
    ("Loose Variance", "Loose", "variance"),
    ("Amount Variance", "Amount", "variance"),
    ("Fine Variance", "Fine", "variance"),
    ("Finest Variance", "Finest", "variance"),
]

#: The two-column statistics.
PAIRED = ("covar_samp", "covar_pop", "corr", "regr_slope", "regr_intercept")

MODEL_YAML = (
    """
version: "1.0"
dataObjects:
  Charges:
    code: charges
    columns:
      Day: {code: day, abstractType: int}
      Qty: {code: qty, abstractType: int}
      Price: {code: price, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      Loose: {code: loose, abstractType: decimal}
      Amount: {code: amount, abstractType: float}
      Fine: {code: fine, abstractType: decimal, sqlPrecision: 38, sqlScale: 37}
      Finest: {code: finest, abstractType: decimal, sqlPrecision: 38, sqlScale: 38}
      Deep: {code: deep, abstractType: decimal, sqlPrecision: 38, sqlScale: 25}
      Deeper: {code: deeper, abstractType: decimal, sqlPrecision: 38, sqlScale: 26}
dimensions:
  Day: {dataObject: Charges, column: Day, resultType: int}
measures:
"""
    + "".join(
        f"  {name}: {{columns: [{{dataObject: Charges, column: {column}}}], "
        f"aggregation: {aggregation}, resultType: float}}\n"
        for name, column, aggregation in STATISTICS
    )
    + "".join(
        f"  Qty {other} {aggregation}: {{columns: [{{dataObject: Charges, column: Qty}}, "
        f"{{dataObject: Charges, column: {other}}}], aggregation: {aggregation}, "
        "resultType: float}\n"
        for other in ("Price", "Loose", "Deep", "Deeper")
        for aggregation in PAIRED
    )
    + """\
  Doubled Variance:
    expression: "{[Charges].[Qty]} * 2"
    aggregation: variance
    resultType: float
  Qty Variance Or Zero:
    columns: [{dataObject: Charges, column: Qty}]
    aggregation: variance
    resultType: float
    defaultValue: 0
metrics:
  Spread Ratio:
    expression: "{[Qty Stddev]} / {[Price Variance]}"
"""
)


def _sql(measure: str, dialect: str = "dremio") -> str:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    select = QuerySelect(dimensions=["Day"], measures=[measure])
    return CompilationPipeline().compile(QueryObject(select=select), model, dialect).sql


#: The exact route's mark: the remainder of the sum of the values' units.
EXACT = "MOD(SUM(CAST("


@pytest.mark.parametrize(
    ("measure", "own", "root"),
    [
        ("Qty Variance", "VARIANCE", False),
        ("Qty Var Pop", "VAR_POP", False),
        ("Qty Stddev", "STDDEV", True),
        ("Qty Stddev Pop", "STDDEV_POP", True),
        ("Doubled Variance", "VARIANCE", False),
    ],
)
def test_dremio_computes_an_integer_statistic_from_exact_moments(
    measure: str, own: str, root: bool
) -> None:
    sql = _sql(measure)

    assert EXACT in sql
    assert ("SQRT(" in sql) is root
    # Past the limit Dremio's own statistic, of the same values.
    assert f'ELSE {own}("Charges"."qty"' in sql
    assert "CAST('1E37' AS DOUBLE)" in sql


def test_a_decimal_is_read_in_units_of_its_last_place() -> None:
    sql = _sql("Price Variance")

    assert 'CAST("Charges"."price" AS DECIMAL(38, 2)) * CAST(\'100\' AS DECIMAL(3, 0))' in sql
    # The limit in the values' own terms, and the variance back in them.
    assert "CAST('1E33' AS DOUBLE)" in sql
    assert "CAST('1E4' AS DOUBLE)" in sql


@pytest.mark.parametrize(
    "measure",
    ["Loose Variance", "Amount Variance"],
    ids=["a_decimal_without_its_places", "a_float"],
)
def test_a_value_of_unknown_places_keeps_dremios_own(measure: str) -> None:
    sql = _sql(measure)

    assert EXACT not in sql
    assert "VARIANCE(" in sql


@pytest.mark.parametrize(
    ("measure", "exact"), [("Fine Variance", True), ("Finest Variance", False)]
)
def test_a_decimal_of_38_places_keeps_dremios_own(measure: str, exact: bool) -> None:
    """Its multiplier, 10^38, has a digit more than Dremio's widest decimal."""
    assert (EXACT in _sql(measure)) is exact


def test_a_default_holds_the_exact_statistic() -> None:
    sql = _sql("Qty Variance Or Zero")

    assert EXACT in sql
    assert sql.count("COALESCE(CAST(CASE WHEN COUNT(") + sql.count("COALESCE(CASE WHEN COUNT(") == 1


def test_a_metric_reads_the_exact_statistics() -> None:
    sql = _sql("Spread Ratio")

    assert sql.count('ELSE STDDEV("Charges"."qty")') == 1
    assert sql.count('ELSE VARIANCE("Charges"."price")') == 1


@pytest.mark.parametrize("dialect", sorted(set(DialectRegistry.available()) - {"dremio", "mysql"}))
def test_the_other_engines_keep_their_own(dialect: str) -> None:
    """Right far from zero there, measured: ``test_statistics_exec``."""
    assert EXACT not in _sql("Qty Variance", dialect)


def test_mysql_sums_in_decimal_65() -> None:
    sql = _sql("Price Variance", "mysql")

    assert "MOD(SUM(CAST(CAST(`Charges`.`price` AS DECIMAL(65, 2))" in sql, sql
    assert "AS DECIMAL(65, 0))" in sql
    # Its limit: sums of squares under 10^63 units, at two places 10^59.
    assert "< CAST('1E59' AS DOUBLE)" in sql
    assert "ELSE VAR_SAMP(`Charges`.`price`)" in sql


@pytest.mark.parametrize(
    ("dialect", "rewritten"),
    [("snowflake", PAIRED), ("dremio", ("covar_samp", "covar_pop")), ("postgres", PAIRED)],
)
def test_a_pair_is_computed_from_exact_moments(dialect: str, rewritten: tuple[str, ...]) -> None:
    for aggregation in rewritten:
        sql = _sql(f"Qty Price {aggregation}", dialect)

        assert EXACT in sql, sql
        # A pair counts only where both of its values are.
        assert 'CASE WHEN "Charges"."price" IS NOT NULL THEN "Charges"."qty" END' in sql
        # Past the limit, the engine's own, as it renders it.
        assert f'{aggregation.upper()}("Charges"."qty", "Charges"."price")' in sql


@pytest.mark.parametrize(
    "dialect",
    sorted(set(DialectRegistry.available()) - {"snowflake", "dremio", "postgres", "mysql"}),
)
def test_the_other_engines_keep_their_own_covariance(dialect: str) -> None:
    """MySQL has none to keep."""
    assert EXACT not in _sql("Qty Price covar_samp", dialect)


def test_postgres_keeps_its_own_numeric_variance() -> None:
    """``NUMERIC``, exact: only its covariance family reads doubles."""
    assert EXACT not in _sql("Qty Variance", "postgres")
    assert EXACT not in _sql("Price Variance", "postgres")


def test_postgres_sums_in_numeric_without_a_limit() -> None:
    """``NUMERIC`` holds any sum: no group keeps the engine's own for its size."""
    sql = _sql("Qty Price covar_samp", "postgres")

    assert (
        'CAST(CAST(CASE WHEN "Charges"."qty" IS NOT NULL THEN "Charges"."price" END AS NUMERIC)'
        in (sql)
    )
    assert "AS DOUBLE PRECISION)" in sql
    assert "AS DOUBLE)" not in sql
    assert "< CAST('1E" not in sql
    assert "COUNT(CASE WHEN" in sql and ") >= 2 THEN" in sql


@pytest.mark.parametrize("dialect", ["snowflake", "dremio"])
def test_a_pair_of_unknown_places_keeps_the_engines_own(dialect: str) -> None:
    assert EXACT not in _sql("Qty Loose covar_samp", dialect)


@pytest.mark.parametrize(("dialect", "bounded"), [("snowflake", True), ("dremio", False)])
def test_snowflake_sums_only_values_under_its_bound(dialect: str, bounded: bool) -> None:
    """Its ``SUM`` past 128 bits raises, in a ``CASE`` branch not taken too."""
    sql = _sql("Qty Price covar_pop", dialect)

    paired = 'CASE WHEN "Charges"."price" IS NOT NULL THEN "Charges"."qty" END'
    bound = f"MAX(ABS(CAST({paired} AS DOUBLE))) < CAST('1E13' AS DOUBLE)"
    assert (bound in sql) is bounded


@pytest.mark.parametrize(
    ("dialect", "measure", "exact"),
    [
        ("snowflake", "Qty Deep covar_samp", True),
        ("snowflake", "Qty Deeper covar_samp", False),
        ("dremio", "Qty Deeper covar_samp", True),
    ],
)
def test_snowflake_keeps_its_own_past_25_places(dialect: str, measure: str, exact: bool) -> None:
    """A value under its bound of 13 digits in units, unscaled by 10^26 or
    more, can pass 128 bits, which Snowflake raises on: a ``DECIMAL(38, 37)``
    of 10^-33 did. Dremio's product rounds instead, past places it has none in."""
    assert (EXACT in _sql(measure, dialect)) is exact


@pytest.mark.parametrize("dialect", ["snowflake", "dremio"])
def test_the_limits_read_the_paired_values(dialect: str) -> None:
    """A value without its pair is in no sum, so it cannot send its group to
    the engine's own statistic: (9e18, NULL) beside a covariance of 18.5 did."""
    sql = _sql("Qty Price covar_samp", dialect)

    paired = 'CAST(CASE WHEN "Charges"."price" IS NOT NULL THEN "Charges"."qty" END AS DOUBLE)'
    assert f"SUM({paired} * {paired})" in sql, sql
    assert 'SUM(CAST("Charges"."qty" AS DOUBLE)' not in sql, sql
