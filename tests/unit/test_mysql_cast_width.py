"""A measure's decimal cast carries at least 38 digits on MySQL.

Every other supported engine refuses a value the target type cannot hold.
MySQL saturates instead and returns the largest value the type can express as
an ordinary row. Measured on the same model and the same data, a true
``100000000000000000`` under ``dataType: "decimal(18, 2)"``:

==========  ===============================================
DuckDB      raises ``Conversion Error``
PostgreSQL  raises ``numeric field overflow``
ClickHouse  raises code 407
Snowflake   raises
Databricks  raises
BigQuery    ``100000000000000000`` - its NUMERIC is (38, 9)
MySQL       ``9999999999999999.99``
==========  ===============================================

MySQL attaches warning 1264 to that row, but a warning is not an error and no
driver on this stack surfaces one, so what reaches a dashboard is a plausible
wrong number - the same class of defect as the zero divisor of #319, and the
worse half of it, because a number flows onward where an error stops.

Nothing at this layer can make MySQL raise: ``STRICT_ALL_TABLES``,
``STRICT_TRANS_TABLES`` and ``TRADITIONAL`` were each measured saturating
exactly as the default does. So the cast is widened rather than guarded, and
the overflow stops being reachable rather than being caught. A range check
around every measure cast would have bought a NULL at the cost of a ``CASE`` on
one dialect and no other.

Values are checked against live engines in
``tests/integration/drift/vendor_exec/test_overflow_cast_exec.py``; what is
asserted here is which types move, which do not, and that no other dialect is
touched. See #336.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import ColumnRef
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.types import DecimalType, SimpleType
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: "1.0"
name: cast_width
dataObjects:
  S:
    code: s
    columns:
      Amt: {code: amt, abstractType: float}
      Qty: {code: qty, abstractType: int}
      Sold On: {code: sold_on, abstractType: date}
dimensions:
  Sale Month: {dataObject: S, column: Sold On, timeGrain: month}
measures:
  Amount: {columns: [{dataObject: S, column: Amt}], resultType: float, aggregation: sum}
  Orders: {columns: [{dataObject: S, column: Qty}], resultType: int, aggregation: sum}
  Sale Count: {columns: [{dataObject: S, column: Qty}], resultType: int, aggregation: count}
  Biggest: {columns: [{dataObject: S, column: Amt}], resultType: float, aggregation: max}
  Wide Amount:
    columns: [{dataObject: S, column: Amt}]
    resultType: float
    aggregation: sum
    dataType: "decimal(50, 2)"
metrics:
  Amount Running:
    type: cumulative
    measure: Amount
    timeDimension: Sale Month
    dataType: "decimal(18, 2)"
"""

DIALECTS = sorted(DialectRegistry.available())
OTHER_DIALECTS = [d for d in DIALECTS if d != "mysql"]


def _sql(measure: str, dialect: str) -> str:
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    return (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=["Sale Month"], measures=[measure])),
            model,
            dialect,
        )
        .sql
    )


def _cast(obml_type: DecimalType | SimpleType, dialect: str = "mysql") -> str:
    dia = DialectRegistry.get(dialect)
    return dia.compile_expr(dia.cast_to_obml_type(ColumnRef(name="x", table="t"), obml_type))


class TestWhatMoves:
    def test_the_default_decimal_is_widened(self) -> None:
        """``decimal(18, 2)`` carries 16 integer digits, which a total reaches."""
        assert "CAST(SUM(`S`.`amt`) AS DECIMAL(38, 2))" in _sql("Amount", "mysql")

    def test_a_wrapper_is_widened_too(self) -> None:
        """The cumulative wrapper types its result through the same call, so it
        cannot be widened on the direct path and left narrow inside a CTE.
        """
        sql = _sql("Amount Running", "mysql")
        assert "DECIMAL(18, 2)" not in sql, sql
        assert sql.count("DECIMAL(38, 2)") == 2, sql


class TestWhatDoesNotMove:
    def test_no_other_dialect_is_touched(self) -> None:
        for dialect in OTHER_DIALECTS:
            sql = _sql("Amount", dialect)
            assert "DECIMAL(38, 2)" not in sql, f"{dialect}: {sql}"

    def test_the_scale_is_carried_through_untouched(self) -> None:
        """The scale is what shapes the value. Only the range bound moves, so a
        model still rounds to the places it asked for.
        """
        for scale in (0, 2, 6, 20):
            assert f", {scale})" in _cast(DecimalType(precision=18, scale=scale))

    def test_a_declared_precision_above_the_floor_is_kept(self) -> None:
        """Widening is a floor, not a rewrite: a model that asked for more than
        38 already said its values are wider than a portable one.
        """
        assert "DECIMAL(50, 2)" in _sql("Wide Amount", "mysql")

    def test_a_precision_beyond_the_engine_is_still_clamped(self) -> None:
        assert "DECIMAL(65, 2)" in _cast(DecimalType(precision=100, scale=2))

    def test_an_integer_target_is_unchanged(self) -> None:
        """``SIGNED`` is MySQL's only 64-bit integer cast target and there is no
        wider one in its CAST vocabulary. A ``SUM`` over a bigint column past
        9223372036854775807 therefore still saturates, and widening it would
        mean casting every count to DECIMAL - changing the type family of the
        most common measure in the model to reach a value no real count has.
        Recorded rather than fixed; see #336.
        """
        assert _cast(SimpleType(name="bigint")) == "CAST(`t`.`x` AS SIGNED)"
        assert "CAST(COUNT(`S`.`qty`) AS SIGNED)" in _sql("Sale Count", "mysql")

    def test_a_pass_through_measure_still_has_no_cast(self) -> None:
        # Scoped to the measure. The statement carries one cast that is not the
        # measure's: MySQL's grain is a DATE_FORMAT, and it is cast back to a
        # DATE so the dimension keeps the type every other dialect gives it.
        sql = _sql("Biggest", "mysql")
        assert "MAX(`S`.`amt`) AS `Biggest`" in sql
        assert "CAST(MAX(" not in sql


@pytest.mark.parametrize(
    ("precision", "scale", "expected"),
    [
        (18, 2, "DECIMAL(38, 2)"),
        (18, 6, "DECIMAL(38, 6)"),
        (18, 0, "DECIMAL(38, 0)"),
        (37, 2, "DECIMAL(38, 2)"),
        (38, 2, "DECIMAL(38, 2)"),
        (38, 20, "DECIMAL(38, 20)"),
        (50, 2, "DECIMAL(50, 2)"),
    ],
)
def test_the_widening_table(precision: int, scale: int, expected: str) -> None:
    assert expected in _cast(DecimalType(precision=precision, scale=scale))
