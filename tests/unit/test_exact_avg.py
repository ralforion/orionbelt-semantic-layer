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

import re
from decimal import Decimal

import pytest

from orionbelt.ast.nodes import ColumnRef
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.type_resolver import _widen_to_integer_range
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.types import DecimalType, parse_data_type
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
    # No exact division at all here, so the average is assembled from integer
    # arithmetic instead: scale, divide with `//`, put the scale back by
    # multiplying (#316).
    "duckdb": "// (2 * CAST(COUNT(",
    # Measured once its workspace was reachable: AVG over BIGINT returns 1.0E18
    # here too. #318 had left it unrewritten on an assumption (#322).
    "databricks": "/ NULLIF(COUNT(",
}
# Postgres, MySQL and Snowflake are already exact, so their *expression* is
# left alone - but their result type is still widened (#330), because an exact
# average the declared type cannot hold is no better than an inexact one.
# The three that need no rewrite because their own AVG is already exact. DuckDB
# was here too, for the opposite reason - no exact division to rewrite *to* -
# until one was assembled from integer arithmetic (#316).
LEFT_ALONE = ["postgres", "mysql", "snowflake"]
NATIVELY_EXACT = ["postgres", "mysql", "snowflake"]


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
        """Not an omission: Postgres, MySQL and Snowflake are already exact.

        They compute ``AVG`` over a 64-bit value exactly, so a rewrite would
        add noise and risk for nothing. DuckDB used to be in this list for the
        opposite reason - no exact division to rewrite *to* - and has since
        been given one built from integer arithmetic (#316).
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


class TestTheResultTypeIsWidenedWhereverTheAverageIsExact:
    """Not only where it is rewritten (#330).

    Postgres, MySQL and Snowflake compute the average exactly and then had it
    cast to the ``decimal(18, 2)`` default, which carries 16 integer digits
    where a 64-bit value needs 19. Measured, MySQL saturated a true
    1000000000000000003 to 9999999999999999.99, and Postgres raised. Being
    exact in the aggregate bought nothing if the type could not carry the
    result.
    """

    #: MySQL floors a measure's decimal cast at 38 digits of its own, because
    #: it saturates an overflow where the others raise (#336). The resolver
    #: still has to widen 18 to 21 - a resolved type that stayed at the default
    #: would show up as a narrower cast on the other two - so what is asserted
    #: per dialect is the width that reaches the SQL.
    WIDENED = {"postgres": "(21, 2)", "snowflake": "(21, 2)", "mysql": "(38, 2)"}

    @pytest.mark.parametrize("dialect", NATIVELY_EXACT)
    def test_a_natively_exact_dialect_widens_its_result(self, dialect: str) -> None:
        sql = _sql("Qty Avg", dialect)
        assert self.WIDENED[dialect] in sql, sql
        assert "(18, 2)" not in sql, sql

    def test_duckdb_is_widened_like_the_rest(self) -> None:
        """It used to be the engine where widening would have hidden the problem.

        Its average was not exact and, at the time, could not be made so, so a
        wider type would have let a rounded value through instead of failing on
        it. Now that the average is assembled exactly (#316), the widened type
        carries a number that deserves it.
        """
        assert "DECIMAL(21, 2)" in _sql("Qty Avg", "duckdb")

    @pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
    def test_every_dialect_is_classified_for_exactness(self, dialect: str) -> None:
        """A new dialect cannot default into "exact" without someone measuring."""
        native = DialectRegistry.get(dialect).avg_over_integers_is_exact
        assert native is (dialect in NATIVELY_EXACT), (
            f"{dialect} claims avg_over_integers_is_exact={native}; measure it before changing"
        )


class TestEveryPathThatCastsAMeasureAgrees:
    """A measure must not change type by being wrapped in a metric.

    ``star`` and ``cfl`` applied the exact-AVG handling; the cumulative,
    period-over-period and window wrappers called ``resolve_measure_data_type``
    alone. So the same integer AVG emitted ``DECIMAL(21, 2)`` queried directly
    and ``DECIMAL(18, 2)`` inside a PoP metric - the type that saturates on
    MySQL and overflows on Postgres.

    All five now share ``cast_measure_to_resolved_type``, which is the point:
    the previous shape let a fix land on two paths and be forgotten on three.
    """

    WRAPPED_YAML = (
        MODEL_YAML.replace(
            "dimensions:\n  Day: {dataObject: Charges, column: Day}",
            "dimensions:\n"
            "  Day: {dataObject: Charges, column: Day}\n"
            "  Sale Month: {dataObject: Charges, column: When, timeGrain: month}",
        ).replace(
            "      Amount: {code: amount, abstractType: float}",
            "      Amount: {code: amount, abstractType: float}\n"
            "      When: {code: when, abstractType: date}",
        )
        + """
metrics:
  Qty Avg Cumul:
    type: cumulative
    measure: Qty Avg
    timeDimension: Sale Month
    cumulativeType: sum
  Qty Avg PoP:
    type: period_over_period
    expression: '{[Qty Avg]}'
    periodOverPeriod:
      timeDimension: Sale Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""
    )

    @pytest.mark.parametrize("measure", ["Qty Avg", "Qty Avg Cumul", "Qty Avg PoP"])
    def test_the_widened_type_survives_every_wrapper(self, measure: str) -> None:
        raw, sm = TrackedLoader().load_string(self.WRAPPED_YAML)
        model, result = ReferenceResolver().resolve(raw, sm)
        assert result.valid, result.errors
        sql = (
            CompilationPipeline()
            .compile(
                QueryObject(select=QuerySelect(dimensions=["Sale Month"], measures=[measure])),
                model,
                "postgres",
            )
            .sql
        )
        assert "DECIMAL(21, 2)" in sql, sql
        assert "DECIMAL(18, 2)" not in sql, f"{measure} kept the saturating default:\n{sql}"


class TestComposedWrappersKeepTheWidenedType:
    """A second wrapper holds a CTE alias, not the aggregate.

    ``rewrite_exact_integer_avg`` can only fire on a bare ``AVG(x)`` - it needs
    the argument to rewrite. That is the right test for rewriting and the wrong
    one for typing: once a window or cumulative metric wraps a
    period-over-period, the later wrapper is casting a column reference into
    the earlier CTE, so the rewrite declined and the cast fell back to
    ``decimal(18, 2)`` - saturating on MySQL, overflowing on Postgres - even
    though the value in that CTE had already been computed exactly.

    The type now follows the measure and the dialect, which are known at every
    site, rather than the shape the expression happens to have.
    """

    COMPOSED_YAML = """
version: "1.0"
name: composed
dataObjects:
  Charges:
    code: charges
    columns:
      When: {code: when, abstractType: date}
      Qty: {code: qty, abstractType: int}
dimensions:
  Sale Month: {dataObject: Charges, column: When, timeGrain: month}
measures:
  Qty Avg: {columns: [{dataObject: Charges, column: Qty}], resultType: int, aggregation: avg}
metrics:
  Qty Avg Prior:
    type: period_over_period
    expression: '{[Qty Avg]}'
    periodOverPeriod:
      timeDimension: Sale Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Qty Avg Lag:
    type: window
    measure: Qty Avg
    windowFunction: lag
    offset: 1
    timeDimension: Sale Month
  Running Qty Avg:
    type: cumulative
    measure: Qty Avg
    timeDimension: Sale Month
    cumulativeType: sum
"""

    def _compile(self, measures: list[str], dialect: str) -> str:
        raw, sm = TrackedLoader().load_string(self.COMPOSED_YAML)
        model, result = ReferenceResolver().resolve(raw, sm)
        assert result.valid, result.errors
        return (
            CompilationPipeline()
            .compile(
                QueryObject(select=QuerySelect(dimensions=["Sale Month"], measures=measures)),
                model,
                dialect,
            )
            .sql
        )

    @pytest.mark.parametrize(
        "measures",
        [
            ["Qty Avg Prior"],
            ["Qty Avg Prior", "Qty Avg Lag"],
            ["Qty Avg Prior", "Running Qty Avg"],
        ],
        ids=["pop", "pop+window", "pop+cumulative"],
    )
    def test_no_wrapper_falls_back_to_the_narrow_default(self, measures: list[str]) -> None:
        sql = self._compile(measures, "postgres")
        assert "DECIMAL(18, 2)" not in sql, sql
        assert "DECIMAL(21, 2)" in sql, sql

    def test_duckdb_is_widened_through_composition_too(self) -> None:
        """What was an exception is now the ordinary path (#316).

        The widened type has to survive composition for the same reason the
        exception had to: a measure must not change type by being wrapped in a
        metric.
        """
        sql = self._compile(["Qty Avg Prior", "Qty Avg Lag"], "duckdb")
        assert "DECIMAL(21, 2)" in sql, sql


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
def test_exactness_is_detected_without_a_second_flag(dialect: str) -> None:
    """A dialect that overrides the rewrite cannot forget to declare itself.

    ``integer_avg_is_exact`` reads the override rather than a parallel boolean,
    so the two cannot disagree.
    """
    from orionbelt.dialect.base import Dialect

    dia = DialectRegistry.get(dialect)
    overrides = type(dia).exact_integer_avg is not Dialect.exact_integer_avg
    assert dia.integer_avg_is_exact() == (dia.avg_over_integers_is_exact or overrides)


class TestNoPathLeavesARawFloatingAverage:
    """The invariant that makes shape-independent widening safe.

    ``resolve_measure_cast_type`` widens whenever the dialect can be exact,
    without inspecting the expression - which is only sound if every path that
    *builds* an integer AVG rewrites it. Two did not, and each left a raw
    floating average with a widened cast around it, which is worse than the
    narrow cast it replaced: the drift became invisible instead of loud.

    - ``defaultValue`` emits ``COALESCE(AVG(x), 0)``, so what reached the
      rewrite was not a bare ``AVG``.
    - A window or cumulative metric's placeholder deliberately skips the
      *cast*, because casting to the metric's own type would truncate the
      window's input - and skipped the rewrite along with it.
    """

    REWRITE_ONLY = ["bigquery", "clickhouse", "dremio", "databricks"]

    SHAPES = {
        "plain": ["Qty Avg"],
        "defaultValue": ["Qty Avg Def"],
        "pop": ["Qty Avg Prior"],
        "window": ["Qty Avg Lag"],
        "pop+window": ["Qty Avg Prior", "Qty Avg Lag"],
    }

    YAML = """
version: "1.0"
name: raw_avg
dataObjects:
  Charges:
    code: charges
    columns:
      When: {code: when, abstractType: date}
      Qty: {code: qty, abstractType: int}
dimensions:
  Sale Month: {dataObject: Charges, column: When, timeGrain: month}
measures:
  Qty Avg: {columns: [{dataObject: Charges, column: Qty}], resultType: int, aggregation: avg}
  Qty Avg Def:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: avg
    defaultValue: 0
metrics:
  Qty Avg Prior:
    type: period_over_period
    expression: '{[Qty Avg]}'
    periodOverPeriod:
      timeDimension: Sale Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: previousValue
  Qty Avg Lag:
    type: window
    measure: Qty Avg
    windowFunction: lag
    offset: 1
    timeDimension: Sale Month
"""

    @pytest.mark.parametrize("dialect", REWRITE_ONLY)
    @pytest.mark.parametrize("shape", sorted(SHAPES), ids=sorted(SHAPES))
    def test_the_aggregate_is_always_rewritten(self, dialect: str, shape: str) -> None:
        raw, sm = TrackedLoader().load_string(self.YAML)
        model, result = ReferenceResolver().resolve(raw, sm)
        assert result.valid, result.errors
        sql = (
            CompilationPipeline()
            .compile(
                QueryObject(
                    select=QuerySelect(dimensions=["Sale Month"], measures=self.SHAPES[shape])
                ),
                model,
                dialect,
            )
            .sql
        )
        raw_avgs = [
            m.group(0)
            for m in re.finditer(r"\b(?:AVG|avg)\(([^()]*)\)", sql)
            if "CAST" not in m.group(1).upper() and "toDecimal" not in m.group(1)
        ]
        assert not raw_avgs, (
            f"{dialect}/{shape} leaves a raw floating average under a widened cast: "
            f"{raw_avgs}\n{sql}"
        )


class TestDuckDBAssemblesItsAverage:
    """The engine with no exact division to divide with (#316).

    Every route through ``/`` returns DOUBLE there - decimal over decimal,
    ``SUM``/``COUNT`` with either operand cast, ``AVG`` over a cast input - so
    the average is assembled from integer arithmetic instead. These execute,
    because the shapes that go wrong in an assembly like this are signs, ties
    and empty groups, and none of them shows up in the rendered SQL.
    """

    #: ``(values, the exact average at scale 2)``. The negatives are the pair a
    #: naive assembly gets wrong: the issue records ``[-3, -2]`` and ``[-3, 2]``
    #: raising a conversion error before this was written carefully.
    CASES = [
        ([9223372036854775807, 9223372036854775805], "9223372036854775806.00"),
        ([5, 0], "2.50"),
        ([-5, 0], "-2.50"),
        ([-3, 2], "-0.50"),
        ([-3, -2], "-2.50"),
        ([1, 0, 0], "0.33"),
        ([-1, 0, 0], "-0.33"),
        ([7], "7.00"),
        ([None, 5, 0], "2.50"),
    ]

    def _value(self, values: list[int | None]):
        """The average of *values* in one group, through the compiled SQL.

        The query groups by ``Day``, so every row carries the same day and the
        result is a single group - which is the point: an assembled average has
        to be right per group, not only over a whole table.
        """
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect()
        con.execute("CREATE TABLE charges (day BIGINT, qty BIGINT, amount DOUBLE)")
        for value in values:
            con.execute("INSERT INTO charges VALUES (1, ?, 1.5)", [value])
        row = con.execute(_sql("Qty Avg", "duckdb")).fetchall()
        con.close()
        return row[0][-1] if row else None

    @pytest.mark.parametrize(("values", "expected"), CASES)
    def test_the_assembled_average_is_exact(self, values, expected) -> None:
        assert str(self._value(values)) == expected

    def test_a_group_with_no_values_is_null_rather_than_a_division(self) -> None:
        """``COUNT`` of zero would raise where ``AVG`` returns NULL.

        A multi-fact plan hits that routinely: a group carrying only another
        fact's rows has no values for this measure at all. Rows of NULL are
        that group - they make a group with a count of zero, where an empty
        table makes no group at all.
        """
        assert self._value([None, None]) is None

    def test_ties_go_away_from_zero_like_the_exact_engines(self) -> None:
        """2.365 and -2.365 at scale 2, where ties-away and ties-to-even differ.

        Measured against PostgreSQL, which is exact natively: it answers 2.37
        and -2.37, and so does this. That is also the rule ``round`` pins.
        """
        rows = [3] * 199 + [473 - 3 * 199]
        assert str(self._value(rows)) == "2.37"
        assert str(self._value([-v for v in rows])) == "-2.37"

    def test_a_float_measure_is_left_alone_here_too(self) -> None:
        """The assembly is exact because the sum is; over a float it is not.

        ``SUM`` of a DOUBLE is a DOUBLE, so scaling and dividing it in integers
        would dress a drifted total as an exact figure.
        """
        assert "//" not in _sql("Amount Avg", "duckdb")

    @pytest.mark.parametrize("scale", [2, 14, 20, 37, 38])
    def test_a_long_fraction_does_not_overflow_the_assembly(self, scale: int) -> None:
        """The scale spends the same 128 bits the total does, so it is capped.

        The sum is multiplied by ``10^s`` *before* the division, so an
        uncapped scale overflows on values the declared type holds
        comfortably: measured, ``decimal(38, 37)`` raised on rows of 5, and
        ``decimal(38, 38)`` asked for a ``DECIMAL(39, 38)`` constant, which is
        not a type and fails at bind time. Beyond the cap the extra places come
        back as zeros, which is the trade the engines that divide already make.
        """
        duckdb = pytest.importorskip("duckdb")
        dialect = DialectRegistry.get("duckdb")
        obml = parse_data_type(f"decimal(38, {scale})")
        expr = dialect.exact_integer_avg(ColumnRef(name="qty"), obml)
        assert expr is not None
        con = duckdb.connect()
        con.execute("CREATE TABLE t (qty BIGINT)")
        con.executemany("INSERT INTO t VALUES (?)", [(5,), (5,)])
        value = con.execute(f"SELECT {dialect.compile_expr(expr)} FROM t").fetchall()[0][0]
        con.close()
        assert Decimal(str(value)) == Decimal(5)

    def test_the_places_beyond_the_cap_are_zeros_rather_than_digits(self) -> None:
        """Honest about what the cap costs: 14 exact places, then padding.

        One third at ``decimal(38, 20)`` reads 0.33333333333333000000 rather
        than twenty threes. The alternative is overflowing on a total the
        source holds legally, which is what the cap exists to avoid.
        """
        duckdb = pytest.importorskip("duckdb")
        dialect = DialectRegistry.get("duckdb")
        obml = parse_data_type("decimal(38, 20)")
        expr = dialect.exact_integer_avg(ColumnRef(name="qty"), obml)
        assert expr is not None
        con = duckdb.connect()
        con.execute("CREATE TABLE t (qty BIGINT)")
        con.executemany("INSERT INTO t VALUES (?)", [(1,), (0,), (0,)])
        sql = dialect.compile_expr(dialect.cast_to_obml_type(expr, obml))
        value = con.execute(f"SELECT {sql} FROM t").fetchall()[0][0]
        con.close()
        assert str(value) == "0.33333333333333000000"
