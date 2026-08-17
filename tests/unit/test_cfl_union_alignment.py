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
      Qty: {code: qty, abstractType: int}
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
  Qty Min:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: min
  Charged Stddev:
    columns: [{dataObject: Charges, column: Amount}]
    resultType: float
    aggregation: stddev
    dataType: "decimal(18, 3)"
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


def _outcome(con: duckdb.DuckDBPyConnection, model: SemanticModel, measures: list[str]) -> str:
    """What *measures* does on *con*: a normalised value, or "failed".

    Failing is an outcome, not an absence of one. The property under test is
    that adding a measure from an independent fact does not change the answer,
    and turning a query that worked into one that raises is exactly that kind
    of change. Comparing values alone would miss it, and treating any raise as
    a failure would flag cases where both paths are equally and legitimately
    out of range - a SUM over a BIGINT whose measure resolves to the
    decimal(18, 2) numeric default overflows on both, which is a default-type
    problem rather than a CFL one.
    """
    try:
        value = _first(con, model, measures)
    except Exception:  # noqa: BLE001 - the outcome is the point
        return "failed"
    return str(Decimal(str(value)).normalize())


def _first(con: duckdb.DuckDBPyConnection, model: SemanticModel, measures: list[str]) -> object:
    """First cell of *measures* compiled against *model* and run on *con*."""
    sql = PIPELINE.compile(
        QueryObject(select=QuerySelect(dimensions=[], measures=measures)), model, "duckdb"
    ).sql
    return con.execute(sql).fetchall()[0][0]


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

    def test_the_owning_leg_carries_the_column_uncast(self) -> None:
        """Rows are carried in the source's own type; only the aggregate narrows.

        The owning leg projects the column with no cast at all. That is what
        makes the narrowing impossible rather than merely wide: there is no
        second type for a row to be squeezed through on its way to the
        aggregate. The NULL pads still carry one, which is what settles the
        union.
        """
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Charged", "Invoiced"])),
            _model(),
            "duckdb",
        ).sql
        assert re.search(r'"(?:Charges|Invoices)"\."amount" AS "(?:Charged|Invoiced)"', sql), (
            f"the owning leg should project the source column uncast:\n{sql}"
        )
        assert not re.search(r'CAST\("(?:Charges|Invoices)"\."amount" AS', sql), (
            f"the owning leg should not cast the source column at all:\n{sql}"
        )
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

    def test_a_selecting_aggregate_is_treated_the_same_way(self) -> None:
        """One mechanism, not two: MIN is carried the same way SUM is.

        This asserted several different things over its life, each matching
        whatever the implementation happened to do. MIN was first left
        unwidened, because widening it at scale 12 turned 1.000000000000400
        into 1.000000000000; then widened along with everything else once the
        width grew.

        Both were workarounds for casting the owning leg at all. Nothing about
        a selecting aggregate needs its own rule now: the column arrives in its
        own type because no cast is applied to it.
        """
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Charged Min", "Invoiced"])),
            _model(),
            "duckdb",
        ).sql
        min_leg = next(line for line in sql.splitlines() if '"Charged Min"' in line)
        assert "CAST" not in min_leg, (
            f"a selecting aggregate should reach the union uncast, like any other: {min_leg}"
        )

    def test_the_width_carries_a_source_wider_than_the_old_default(self) -> None:
        """A DECIMAL(38, 15) source is the case the previous width lost.

        Twelve places was a guess and it was too small; twenty carries
        realistic money and rate columns, measured exact on Postgres, DuckDB,
        ClickHouse, Snowflake and BigQuery. It is still a guess, and a model
        that declares sqlPrecision/sqlScale is taken at its word instead.
        """
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TABLE days (day INTEGER)")
            con.execute(
                "CREATE TABLE charges (day INTEGER, amount DECIMAL(38, 15), "
                "label VARCHAR, qty BIGINT)"
            )
            con.execute("CREATE TABLE invoices (day INTEGER, amount DECIMAL(18, 6))")
            con.execute("INSERT INTO days VALUES (1), (2)")
            con.execute(
                "INSERT INTO charges VALUES "
                "(1, 1.000000000000400, 'x', 1000000000000000001), (2, 2.5, 'y', 2)"
            )
            con.execute("INSERT INTO invoices VALUES (1, 3.5), (2, 4.5)")
            star = _run(con, ["Charged Min"])[0][0]
            cfl = _run(con, ["Charged Min", "Invoiced"])[0][0]
        finally:
            con.close()
        assert Decimal(str(star)) == Decimal("1.000000000000400")
        assert Decimal(str(cfl)) == Decimal(str(star)), (
            "the alignment width was too narrow for the source column"
        )

    def test_a_statistical_aggregate_accumulates_too(self) -> None:
        """STDDEV combines values across rows, so it rounds the same way SUM does.

        A first cut listed only SUM and AVG, on the reasoning that they were
        the obvious accumulating pair. STDDEV, STDDEV_POP, VARIANCE and VAR_POP
        are single-column aggregates over the same pre-aggregation rows and
        compound the same error: over 1.0004 and 1.0005 declared
        ``decimal(18, 3)``, STDDEV answered 0.000 alone and 0.001 beside a
        measure from another fact.

        The two-column statistics (CORR, COVAR_*, REGR_*) never reach this
        path: a multi-field measure pads per slot and keeps each slot's type.
        """
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Charged Stddev", "Invoiced"])),
            _model(),
            "duckdb",
        ).sql
        leg = next(line for line in sql.splitlines() if '"Charged Stddev"' in line)
        assert "CAST" not in leg, (
            f"a statistical aggregate narrowed its rows before aggregating: {leg}"
        )


class TestStarAndCflAgreeAcrossTheMatrix:
    """The property, asserted over every shape rather than case by case.

    This function was wrong three times running, each time for ``expression:``
    measures and each time in a different clause: missed by the accumulating
    widening, then by an attempt to leave the union untyped, then by the
    integer alignment. Every one was found by review rather than by a test,
    because the tests were written per case and the cases were the ones already
    known to be broken.

    A matrix cannot be written that way. It covers the product of source shape,
    source kind and aggregation, so a clause that handles one cell and forgets
    its neighbour fails here rather than in review.

    The declaration axis is here for the same reason. ``sqlPrecision`` and
    ``sqlScale`` are independently optional, and narrowing on a precision whose
    scale was merely assumed to be 0 reintroduced the original rounding bug.
    """

    SOURCES = {
        ("columns", "float"): "    columns: [{dataObject: Charges, column: Wide}]",
        ("expression", "float"): '    expression: "{[Charges].[Wide]} * 1"',
        ("columns", "int"): "    columns: [{dataObject: Charges, column: Big}]",
        ("expression", "int"): '    expression: "{[Charges].[Big]} * 1"',
        # Declared width, both halves and each half alone.
        ("declared-both", "float"): "    columns: [{dataObject: Charges, column: DeclBoth}]",
        ("declared-precision", "float"): ("    columns: [{dataObject: Charges, column: DeclPrec}]"),
        ("declared-scale", "float"): "    columns: [{dataObject: Charges, column: DeclScale}]",
    }
    AGGREGATIONS = ["sum", "avg", "stddev", "variance", "min", "max", "any_value"]

    def _model_for(self, key: tuple[str, str], agg: str) -> SemanticModel:
        result_type = key[1]
        yaml = MODEL_YAML.replace(
            "measures:",
            "measures:\n  Probe:\n"
            f"{self.SOURCES[key]}\n"
            f"    resultType: {result_type}\n"
            f"    aggregation: {agg}\n",
            1,
        ).replace(
            "      Amount: {code: amount, abstractType: float}",
            "      Amount: {code: amount, abstractType: float}\n"
            "      Wide: {code: wide, abstractType: float}\n"
            "      Big: {code: big, abstractType: int}\n"
            "      DeclBoth: {code: wide, abstractType: float, "
            "sqlPrecision: 38, sqlScale: 15}\n"
            "      DeclPrec: {code: wide, abstractType: float, sqlPrecision: 38}\n"
            "      DeclScale: {code: wide, abstractType: float, sqlScale: 15}",
            1,
        )
        raw, source_map = TrackedLoader().load_string(yaml)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def test_every_shape_answers_the_same_alone_and_multi_fact(self) -> None:
        disagreements = []
        for key in self.SOURCES:
            for agg in self.AGGREGATIONS:
                model = self._model_for(key, agg)
                con = duckdb.connect(":memory:")
                try:
                    con.execute("CREATE TABLE days (day INTEGER)")
                    con.execute(
                        "CREATE TABLE charges (day INTEGER, amount DECIMAL(18, 6), "
                        "label VARCHAR, qty BIGINT, wide DECIMAL(38, 15), big BIGINT)"
                    )
                    con.execute("CREATE TABLE invoices (day INTEGER, amount DECIMAL(18, 6))")
                    con.execute("INSERT INTO days VALUES (1), (2), (3)")
                    con.execute(
                        "INSERT INTO charges VALUES "
                        "(1, 1.0004, 'x', 1, 1.000000000000400, 1000000000000000001), "
                        "(2, 1.0005, 'y', 2, 2.000000000000500, 1000000000000000002), "
                        # A DECIMAL(38, 15) using its full integer range. Any
                        # fixed-width decimal alignment either rounds the
                        # fraction off this or overflows on the integer part.
                        "(3, 1.0006, 'z', 3, 1000000000000000000.000000000000001, "
                        "1000000000000000003)"
                    )
                    con.execute("INSERT INTO invoices VALUES (1, 3.5), (2, 4.5), (3, 5.5)")
                    star = _outcome(con, model, ["Probe"])
                    cfl = _outcome(con, model, ["Probe", "Invoiced"])
                finally:
                    con.close()
                if star != cfl:
                    disagreements.append(f"{key[0]}/{key[1]}/{agg}: star={star} cfl={cfl}")
        assert not disagreements, (
            "a measure changed answer under a multi-fact plan:\n  " + "\n  ".join(disagreements)
        )
