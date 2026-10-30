"""Exact ``variance`` and ``stddev`` of integers and decimals on Dremio.

Dremio's statistics subtract sums of squares as doubles and cancel far from
zero: 0 for the variance of 10^9 + 0..9, a negative one further out. They are
computed from exact moments there instead (``Dialect.exact_variance``). These
tests pin which measures are rewritten and the shape of the SQL; the vendor
suite (``test_exact_variance_exec``) runs it against the exact statistic.
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
dimensions:
  Day: {dataObject: Charges, column: Day, resultType: int}
measures:
"""
    + "".join(
        f"  {name}: {{columns: [{{dataObject: Charges, column: {column}}}], "
        f"aggregation: {aggregation}, resultType: float}}\n"
        for name, column, aggregation in STATISTICS
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


@pytest.mark.parametrize("dialect", sorted(set(DialectRegistry.available()) - {"dremio"}))
def test_the_other_engines_keep_their_own(dialect: str) -> None:
    """Right far from zero there, measured: ``test_statistics_exec``."""
    assert EXACT not in _sql("Qty Variance", dialect)
