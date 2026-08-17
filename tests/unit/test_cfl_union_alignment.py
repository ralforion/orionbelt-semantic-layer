"""What type a CFL UNION leg carries a measure's rows in.

A leg projects **pre-aggregation rows** and the outer query aggregates them, so
the type the legs agree on is not the type the result has. Aligning them on the
measure's declared ``dataType`` conflated the two and rounded every row before
it was summed: a measure declared ``decimal(18, 2)`` over a six-decimal column
lost up to 0.005 per row, which is invisible on one row and material on fifteen
thousand.

That made the same measure answer differently depending on whether the query
happened to be multi-fact, which for a semantic layer is the cardinal sin: a
measure means one thing regardless of what is selected alongside it. Issue #305.

The alignment cast itself has to stay. A strict engine will not union columns
whose types disagree - ClickHouse builds a ``Variant(Decimal, Float64)`` and
then refuses to ``SUM`` it, measured as error code 43 - so the fix is the type
chosen, not the casting.
"""

from __future__ import annotations

import re
from decimal import Decimal

import duckdb

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

PIPELINE = CompilationPipeline()

# Two independent facts meeting only at a conformed dimension, each carrying a
# money column with more scale than the measure declares. That gap is the whole
# subject: without it the rounding has nothing to round away.
MODEL_YAML = """\
version: 1.0

settings:
  defaultDialect: duckdb

dataObjects:
  Days:
    code: days
    schema: main
    columns:
      Day: {code: day, abstractType: int, primaryKey: true}

  Charges:
    code: charges
    schema: main
    columns:
      Day: {code: day, abstractType: int}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Days
        columnsFrom: [Day]
        columnsTo: [Day]

  Invoices:
    code: invoices
    schema: main
    columns:
      Day: {code: day, abstractType: int}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Days
        columnsFrom: [Day]
        columnsTo: [Day]

dimensions:
  Day: {dataObject: Days, column: Day, resultType: int}

measures:
  Charged Expr:
    expression: "{[Charges].[Amount]} * 1"
    resultType: float
    aggregation: sum
    dataType: "decimal(18, 2)"
  Charged Min:
    columns: [{dataObject: Charges, column: Amount}]
    resultType: float
    aggregation: min
  Charged:
    columns: [{dataObject: Charges, column: Amount}]
    resultType: float
    aggregation: sum
    dataType: "decimal(18, 2)"
  Invoiced:
    columns: [{dataObject: Invoices, column: Amount}]
    resultType: float
    aggregation: sum
    dataType: "decimal(18, 2)"
"""

# Every row is a third of a cent away from a clean value, so per-row rounding
# is both certain and cumulative rather than luck of the data.
ROWS = 300
FRACTION = Decimal("0.004")


def _model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _seeded() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE days (day INTEGER)")
    con.execute("CREATE TABLE charges (day INTEGER, amount DECIMAL(18, 6))")
    con.execute("CREATE TABLE invoices (day INTEGER, amount DECIMAL(18, 6))")
    con.execute(f"INSERT INTO days SELECT * FROM range(1, {ROWS + 1})")
    for table in ("charges", "invoices"):
        con.execute(f"INSERT INTO {table} SELECT r, 1 + {FRACTION} FROM range(1, {ROWS + 1}) t(r)")
    return con


def _run(con: duckdb.DuckDBPyConnection, measures: list[str]) -> list[tuple]:
    sql = PIPELINE.compile(
        QueryObject(select=QuerySelect(dimensions=[], measures=measures)),
        _model(),
        "duckdb",
    ).sql
    return con.execute(sql).fetchall()


class TestUnionAlignmentPreservesInput:
    def test_a_measure_answers_the_same_multi_fact_as_alone(self) -> None:
        """The regression #305 describes, stated as the property it violates.

        ``Charged`` alone takes the star path and casts after ``SUM``; asking
        for it beside a measure from an independent fact takes the CFL path.
        The two must agree, and did not: over 300 rows of 1.004 the multi-fact
        answer came back 1.20 short.
        """
        con = _seeded()
        try:
            star = _run(con, ["Charged"])[0][0]
            cfl = _run(con, ["Charged", "Invoiced"])[0][0]
        finally:
            con.close()
        expected = (Decimal(1) + FRACTION) * ROWS
        assert Decimal(str(star)) == expected.quantize(Decimal("0.01"))
        assert Decimal(str(cfl)) == Decimal(str(star)), (
            "the same measure answered differently under a multi-fact plan"
        )

    def test_the_legs_align_on_a_wider_type_than_the_result(self) -> None:
        """Rows are carried wide; only the aggregate narrows to the declared type.

        Asserted on the shape rather than the exact spelling, since the width
        is a fallback the model can override with ``sqlPrecision``/``sqlScale``.
        """
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Charged", "Invoiced"])),
            _model(),
            "duckdb",
        ).sql
        leg_casts = re.findall(r'CAST\("(?:Charges|Invoices)"\."amount" AS ([A-Z0-9_(), ]+)\)', sql)
        assert leg_casts, f"expected a leg cast on the source column:\n{sql}"
        for rendered in leg_casts:
            scale = int(re.search(r",\s*(\d+)\)", rendered).group(1))
            assert scale > 2, f"leg narrowed rows to the result's scale: {rendered}"
        assert re.search(r"CAST\(SUM\(.*?\) AS DECIMAL\(18, 2\)\)", sql), (
            f"the aggregate should still narrow to the declared dataType:\n{sql}"
        )

    def test_every_leg_agrees_on_one_type(self) -> None:
        """The reason the cast exists at all.

        ClickHouse will not union columns whose types disagree: it builds a
        ``Variant(Decimal, Float64)`` and refuses to ``SUM`` it. Widening the
        own-measure cast without widening the NULL padding to match would trade
        this bug for that one.
        """
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=["Day"], measures=["Charged", "Invoiced"])),
            _model(),
            "clickhouse",
        ).sql
        # Only the union legs, not the outer aggregate: the whole point is that
        # those two carry different types, so searching the statement as a
        # whole would find the difference the fix introduces on purpose.
        legs = sql[sql.index("WITH ") : sql.rindex(")\nSELECT")]
        types = set(re.findall(r"AS (Nullable\(Decimal\(\d+, \d+\)\))", legs))
        assert len(types) == 1, f"legs disagree on the union type: {types}\n{legs}"
        assert re.search(r"CAST\(round\(SUM\(.*?\), 2\) AS Nullable\(Decimal\(18, 2\)\)\)", sql), (
            f"the outer aggregate should still narrow to the declared type:\n{sql}"
        )


class TestWhatIsAndIsNotWidened:
    """Widening follows what an aggregation *does*, not what it is over."""

    def test_an_expression_measure_is_covered_too(self) -> None:
        """It has no ``columns``, and projects a pre-aggregation expression.

        An earlier version keyed on having exactly one column, so every
        ``expression:`` measure kept the original bug while the tests, which
        only covered ``columns:`` measures, stayed green.
        """
        con = _seeded()
        try:
            star = _run(con, ["Charged Expr"])[0][0]
            cfl = _run(con, ["Charged Expr", "Invoiced"])[0][0]
        finally:
            con.close()
        assert Decimal(str(cfl)) == Decimal(str(star)), (
            "an expression measure answered differently under a multi-fact plan"
        )

    def test_a_selecting_aggregate_is_left_alone(self) -> None:
        """MIN picks a value rather than combining values, so nothing compounds.

        ``resolve_measure_data_type`` treats MIN, MAX, ANY_VALUE, MEDIAN and
        MODE as no-cast pass-through. Routing them through the widening changed
        both the value and its type - MIN over a fifteen-decimal column went
        from 1.000000000000400 to 1.000000000000 - so they keep the existing
        alignment.

        This asserts the leg is not widened rather than asserting a value:
        those aggregates have a separate, older precision problem of their own
        (the leg casts a wide decimal to FLOAT), which this change neither
        causes nor fixes.
        """
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Charged Min", "Invoiced"])),
            _model(),
            "duckdb",
        ).sql
        min_leg = next(
            line for line in sql.splitlines() if '"Charged Min"' in line and "CAST" in line
        )
        assert "DECIMAL(38, 12)" not in min_leg, (
            f"a selecting aggregate was widened as though it accumulated: {min_leg}"
        )
