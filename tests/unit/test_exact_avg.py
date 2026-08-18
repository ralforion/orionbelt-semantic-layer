"""Exact ``AVG`` over integer measures, where the engine can manage one.

``AVG`` is a floating-point aggregate on BigQuery, ClickHouse, Dremio and
DuckDB whatever the input type, so it drifts once the average passes a
``double`` mantissa - about fifteen significant digits. The loss is inside the
aggregate, so no output cast repairs it, which is why declaring ``dataType``
was measured to make things worse rather than better (#315).

Three of those four can be asked for exact arithmetic instead, by three
different routes, and the fourth cannot. These tests pin which dialects are
rewritten, which are deliberately left alone, and the shape each one emits.
Execution against live engines lives in the vendor suites; what is asserted
here is the decision and the SQL.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.type_resolver import _widen_to_integer_range
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.types import DecimalType
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: "1.0"
name: exact_avg
dataObjects:
  Charges:
    code: charges
    columns:
      Day: {code: day, abstractType: int}
      Qty: {code: qty, abstractType: int}
      Amount: {code: amount, abstractType: float}
measures:
  Qty Avg:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: avg
  Qty Avg Declared:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: avg
    dataType: "decimal(30, 4)"
  Qty Avg Distinct:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: avg
    distinct: true
  Amount Avg:
    columns: [{dataObject: Charges, column: Amount}]
    resultType: float
    aggregation: avg
  Qty Sum:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: sum
dimensions:
  Day: {dataObject: Charges, column: Day}
"""

# The three that get a rewrite, and the route each one takes. They are all
# different, which is the reason this could not be one change: casting the
# input works only on BigQuery, ordinary decimal division only on Dremio, and
# ClickHouse needs a function of its own.
REWRITTEN = {
    "bigquery": "AVG(CAST(",
    "clickhouse": "divideDecimal(",
    "dremio": "/ NULLIF(COUNT(",
    # Measured once its workspace was reachable: AVG over BIGINT returns 1.0E18
    # here too. #318 had left it unrewritten on an assumption (#322).
    "databricks": "/ NULLIF(COUNT(",
}
# Postgres, MySQL and Snowflake are already exact; DuckDB has no exact
# division at all, so a rewrite there would only trade a loud overflow for a
# quiet wrong number (#316).
LEFT_ALONE = ["duckdb", "postgres", "mysql", "snowflake"]


def _model():
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    return model


def _sql(measure: str, dialect: str) -> str:
    return (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=["Day"], measures=[measure])),
            _model(),
            dialect,
        )
        .sql
    )


class TestWhichDialectsAreRewritten:
    @pytest.mark.parametrize(("dialect", "marker"), sorted(REWRITTEN.items()))
    def test_an_integer_avg_is_rewritten_exactly(self, dialect: str, marker: str) -> None:
        sql = _sql("Qty Avg", dialect)
        assert marker in sql, f"{dialect} should render an exact average:\n{sql}"

    @pytest.mark.parametrize("dialect", LEFT_ALONE)
    def test_the_others_keep_the_plain_avg(self, dialect: str) -> None:
        """Not an omission: two different reasons, both deliberate.

        Postgres, MySQL and Snowflake compute ``AVG`` over a 64-bit value
        exactly already, so a rewrite would add noise and risk for nothing.
        DuckDB has no exact division to rewrite *to* - every route through
        ``/`` returns DOUBLE - so the only thing a widened result would buy is
        a plausible wrong number in place of an error.
        """
        sql = _sql("Qty Avg", dialect)
        assert "AVG(" in sql.upper(), sql
        assert "divideDecimal" not in sql and "NULLIF(COUNT(" not in sql, sql


class TestWhatIsAndIsNotEligible:
    @pytest.mark.parametrize("dialect", sorted(REWRITTEN))
    def test_a_float_measure_is_untouched(self, dialect: str) -> None:
        """The gate that keeps BigQuery honest.

        BigQuery's route casts the *input* to NUMERIC, which is (38, 9). A
        float column carrying more than nine decimal places would be truncated
        by that cast and a large one would overflow it, so the fix would swap
        one silent error for another. The rewrite is for integer sources only.
        """
        sql = _sql("Amount Avg", dialect)
        assert "divideDecimal" not in sql and "NULLIF(COUNT(" not in sql, sql
        assert "AVG(CAST(" not in sql, sql

    @pytest.mark.parametrize("dialect", sorted(REWRITTEN))
    def test_a_distinct_average_is_untouched(self, dialect: str) -> None:
        """``SUM``/``COUNT`` over DISTINCT is not the same average."""
        sql = _sql("Qty Avg Distinct", dialect)
        assert "divideDecimal" not in sql and "NULLIF(COUNT(" not in sql, sql

    @pytest.mark.parametrize("dialect", sorted(REWRITTEN))
    def test_a_sum_is_not_an_average(self, dialect: str) -> None:
        sql = _sql("Qty Sum", dialect)
        assert "divideDecimal" not in sql and "NULLIF(COUNT(" not in sql, sql


class TestTheResultTypeMovesWithTheExpression:
    def test_an_inferred_type_is_widened_to_hold_the_exact_answer(self) -> None:
        """Otherwise the exact average overflows the cast describing it.

        The default is ``decimal(18, 2)``, which carries 16 integer digits
        where a 64-bit value needs 19. Computing the average exactly and then
        failing to cast it would be a strange kind of progress.
        """
        assert "DECIMAL(21, 2)" in _sql("Qty Avg", "dremio")

    def test_a_declared_type_is_left_as_declared(self) -> None:
        """An explicit ``dataType`` wins, as the resolution order promises."""
        sql = _sql("Qty Avg Declared", "dremio")
        assert "DECIMAL(30, 4)" in sql, sql
        assert "/ NULLIF(COUNT(" in sql, sql


class TestWidening:
    """Precision moves first; scale gives way only when it must."""

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (DecimalType(18, 2), DecimalType(21, 2)),
            (DecimalType(18, 6), DecimalType(25, 6)),
            (DecimalType(38, 2), DecimalType(38, 2)),
            # 19 + 20 needs 39 digits, past what any engine accepts. Clamping
            # the precision alone left 18 integer digits and still overflowed,
            # so the scale has to give instead.
            (DecimalType(38, 20), DecimalType(38, 19)),
        ],
    )
    def test_widening_always_leaves_room_for_a_64_bit_integer(
        self, given: DecimalType, expected: DecimalType
    ) -> None:
        widened = _widen_to_integer_range(given)
        assert widened == expected
        assert widened.precision - widened.scale >= 19


def test_every_dialect_is_accounted_for() -> None:
    """No dialect quietly escapes the decision.

    A new dialect should be measured and placed in one list or the other
    rather than defaulting into "not rewritten" without anyone looking.
    """
    known = set(REWRITTEN) | set(LEFT_ALONE)
    assert set(DialectRegistry.available()) == known, (
        f"unclassified dialects: {set(DialectRegistry.available()) ^ known}"
    )


class TestTheHazardsAPlainAvgHandlesAndARewriteMustToo:
    """Three ways a naive rewrite answers where ``AVG`` would have been right.

    Each was found by review rather than by the first round of tests, and each
    is a case where the rewrite is not merely less precise but *wrong* - which
    is worse than the drift it was written to fix. They are asserted on the
    emitted SQL here; the values they produce are checked against live engines
    in the vendor suites.
    """

    def test_clickhouse_guards_an_empty_group(self) -> None:
        """``divideDecimal`` by zero raises where ``AVG`` returns NULL.

        A multi-fact plan hits this routinely: a group carrying only another
        fact's rows has no values for this measure, so the count is zero.
        ClickHouse answers ILLEGAL_DIVISION (code 153) rather than NULL.
        """
        sql = _sql("Qty Avg", "clickhouse")
        assert "if(COUNT(" in sql and "= 0" in sql, sql

    @pytest.mark.parametrize(
        ("dialect", "inner"),
        [("clickhouse", "SUM(toDecimal128("), ("dremio", "SUM(CAST(")],
    )
    def test_the_widening_cast_goes_inside_the_sum(self, dialect: str, inner: str) -> None:
        """Otherwise the accumulator overflows before anything widens it.

        ``SUM`` over a 64-bit column accumulates in 64 bits on both engines:
        two rows of 9000000000000000000 summed to -446744073709551616, and
        casting that afterwards only widens a number that had already wrapped.
        On Dremio this is a case where the engine's own ``AVG`` is wrong too,
        so the rewrite fixes more than the drift it was written for.
        """
        assert inner in _sql("Qty Avg", dialect), _sql("Qty Avg", dialect)

    def test_bigquery_aggregates_in_bignumeric_when_the_scale_needs_it(self) -> None:
        """NUMERIC is (38, 9), so it caps the quotient at nine places.

        Averaging 1, 2 and 2 in NUMERIC gives 1.666666667 where BIGNUMERIC
        gives 1.66666666666666666666666666666666666667. Asking for more scale
        than NUMERIC carries and aggregating in it anyway would hand back the
        extra digits as zeros.
        """
        assert "AS NUMERIC" in _sql("Qty Avg", "bigquery")
        assert "AS BIGNUMERIC" in _sql_with_default("Qty Avg", "bigquery", "decimal(38, 20)")

    def test_dremio_keeps_integer_room_for_the_running_total(self) -> None:
        """A sum is as many digits as its rows make it.

        38 digits cannot hold both a large total and a long fraction, so the
        intermediate scale is capped rather than tracking the result's. At a
        declared scale of 19 the total would have had 19 integer digits, and
        two rows of 9000000000000000000 need 20.
        """
        sql = _sql_with_default("Qty Avg", "dremio", "decimal(38, 20)")
        assert "AS DECIMAL(38, 14))" in sql, sql
        assert "AS DECIMAL(38, 19))" in sql, sql


def _sql_with_default(measure: str, dialect: str, numeric_default: str) -> str:
    yaml = MODEL_YAML.replace(
        "name: exact_avg",
        f'name: exact_avg\nsettings: {{defaultNumericDataType: "{numeric_default}"}}',
    )
    raw, sm = TrackedLoader().load_string(yaml)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    return (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=["Day"], measures=[measure])), model, dialect
        )
        .sql
    )
