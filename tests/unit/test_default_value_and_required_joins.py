"""Two things the model can now state that the engine used to decide.

``Measure.defaultValue`` says what an aggregate over nothing reads as, and
``DataObjectJoin.required`` says whether an unmatched row survives the join.
Both exist because the alternative is per-dialect behaviour a model author
cannot see: an aggregate over an empty row set is NULL in standard SQL and 0
on ClickHouse, and the ``IS NOT NULL`` filter that stands in for an inner join
keeps every row on ClickHouse rather than dropping the unmatched ones.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

PIPELINE = CompilationPipeline()

ALL_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]

MODEL_YAML = """\
version: 1.0

dataObjects:
  Store:
    code: STORE
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: S_KEY, abstractType: int, primaryKey: true}
      Store Name: {code: S_NAME, abstractType: string}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: SS_STORE_SK, abstractType: int}
      Status: {code: STATUS, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Store Key]
        columnsTo: [Store Key]

dimensions:
  Store Name: {dataObject: Store, column: Store Name, resultType: string}

measures:
  Plain Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Zero Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    defaultValue: 0
    filters:
      - column: {dataObject: Sales, column: Status}
        operator: equals
        values: [{dataType: string, valueString: open}]
  Labelled Count:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: int
    aggregation: count
    defaultValue: none
"""

REQUIRED_JOIN = MODEL_YAML.replace(
    """        columnsFrom: [Store Key]
        columnsTo: [Store Key]""",
    """        columnsFrom: [Store Key]
        columnsTo: [Store Key]
        required: true""",
)


def _load(yaml_str: str = MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_str)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert SemanticValidator().validate(model) == []
    return model


def _sql(yaml_str: str, measures: list[str], dialect: str = "postgres") -> str:
    return PIPELINE.compile(
        QueryObject(select=QuerySelect(dimensions=["Store Name"], measures=measures)),
        _load(yaml_str),
        dialect,
    ).sql


class TestDefaultValue:
    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_wraps_the_aggregate_on_every_dialect(self, dialect: str) -> None:
        """The point of declaring it is that the answer stops depending on the
        engine, so every dialect has to emit the same normalisation."""
        sql = _sql(MODEL_YAML, ["Zero Amount"], dialect)
        assert "COALESCE(" in sql.upper(), dialect

    def test_wraps_outside_not_inside(self) -> None:
        """``COALESCE(SUM(x), 0)`` answers 0 when the aggregate saw nothing;
        ``SUM(COALESCE(x, 0))`` answers 0 for a row whose value is missing.
        Different claims, and only the first is what the field means."""
        sql = _sql(MODEL_YAML, ["Zero Amount"])
        assert "COALESCE(SUM(" in sql
        assert "SUM(COALESCE(" not in sql

    def test_unset_keeps_the_standard_null(self) -> None:
        sql = _sql(MODEL_YAML, ["Plain Amount"])
        assert "COALESCE" not in sql

    def test_applies_to_an_unfiltered_measure_too(self) -> None:
        """Nothing ties it to filters: an unfiltered aggregate over an empty
        row set is equally NULL, and equally 0 on ClickHouse."""
        yaml_str = MODEL_YAML.replace(
            """  Plain Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum""",
            """  Plain Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    defaultValue: 0""",
        )
        assert "COALESCE(SUM(" in _sql(yaml_str, ["Plain Amount"])

    def test_a_non_numeric_default_is_quoted_as_a_literal(self) -> None:
        sql = _sql(MODEL_YAML, ["Labelled Count"])
        assert "'none'" in sql


class TestRequiredJoin:
    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_emits_inner_join_on_every_dialect(self, dialect: str) -> None:
        sql = _sql(REQUIRED_JOIN, ["Plain Amount"], dialect)
        assert "INNER JOIN" in sql, dialect
        assert "LEFT JOIN" not in sql, dialect

    def test_default_is_still_left(self) -> None:
        sql = _sql(MODEL_YAML, ["Plain Amount"])
        assert "LEFT JOIN" in sql
        assert "INNER JOIN" not in sql

    def test_join_condition_is_unchanged(self) -> None:
        """Only the join *type* moves — the keys it matches on are the same."""
        on = 'ON "Sales"."SS_STORE_SK" = "Store"."S_KEY"'
        assert on in _sql(MODEL_YAML, ["Plain Amount"])
        assert on in _sql(REQUIRED_JOIN, ["Plain Amount"])


class TestExecuted:
    """What the SQL actually does, run on DuckDB."""

    @staticmethod
    def _run(sql: str) -> list[tuple]:
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute('CREATE SCHEMA "PUBLIC"')
        con.execute('CREATE TABLE "PUBLIC"."STORE" (S_KEY INTEGER, S_NAME VARCHAR)')
        con.execute(
            'CREATE TABLE "PUBLIC"."SALES" (SS_STORE_SK INTEGER, STATUS VARCHAR, AMOUNT DOUBLE)'
        )
        con.execute('INSERT INTO "PUBLIC"."STORE" VALUES (1, \'Main\')')
        # One matched row whose status excludes it from the filtered measure,
        # and one row whose store key matches nothing.
        con.execute(
            "INSERT INTO \"PUBLIC\".\"SALES\" VALUES (1, 'closed', 10.0), (99, 'closed', 5.0)"
        )
        return con.execute(sql).fetchall()

    def test_default_value_replaces_the_null(self) -> None:
        rows = dict(
            (name, value)
            for name, value in [
                (r[0], r[1]) for r in self._run(_sql(MODEL_YAML, ["Zero Amount"], "duckdb"))
            ]
        )
        assert rows["Main"] == 0, rows  # no 'open' rows — 0, not NULL

    def test_without_a_default_the_group_reads_null(self) -> None:
        yaml_str = MODEL_YAML.replace("    defaultValue: 0\n", "")
        rows = self._run(_sql(yaml_str, ["Zero Amount"], "duckdb"))
        assert [r for r in rows if r[0] == "Main"][0][1] is None

    def test_required_join_drops_the_unmatched_row(self) -> None:
        left = self._run(_sql(MODEL_YAML, ["Plain Amount"], "duckdb"))
        inner = self._run(_sql(REQUIRED_JOIN, ["Plain Amount"], "duckdb"))
        # The unmatched sale groups under a NULL store name with LEFT, and is
        # gone entirely with the join declared required.
        assert None in [r[0] for r in left]
        assert None not in [r[0] for r in inner]
        assert sum(r[1] for r in inner) == 10.0


# ---------------------------------------------------------------------------
# The default must not be mistaken for part of the aggregate
# ---------------------------------------------------------------------------

MULTI_FACT = """\
version: 1.0

dataObjects:
  Dates:
    code: DATES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int, primaryKey: true}
      Month: {code: MONTH, abstractType: int}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

  Refunds:
    code: REFUNDS
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Refund: {code: REFUND, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

dimensions:
  Month: {dataObject: Dates, column: Month, resultType: int}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    defaultValue: 0
  Refund Amount:
    columns: [{dataObject: Refunds, column: Refund}]
    resultType: float
    aggregation: sum
"""


class TestDefaultValueInAMultiFactPlan:
    """CFL spreads a multi-argument aggregate across its union legs, one column
    per argument. A declared default presents as ``COALESCE(agg, default)``,
    which is two arguments and is not that — the legs projected a bare ``0`` as
    a column and the outer query concatenated it into a string.
    """

    def _sql(self) -> str:
        return PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Month"], measures=["Sales Amount", "Refund Amount"])
            ),
            _load(MULTI_FACT),
            "duckdb",
        ).sql

    def test_plan_is_multi_fact(self) -> None:
        assert "UNION ALL" in self._sql()

    def test_legs_project_the_aggregate_input_not_the_default(self) -> None:
        legs = self._sql().split("UNION ALL")[0]
        assert '"Sales"."AMOUNT"' in legs
        assert "__f0" not in legs and "__f1" not in legs

    def test_the_default_lands_on_the_outer_rebuild(self) -> None:
        assert 'COALESCE(SUM("composite_01"."Sales Amount"), 0)' in self._sql()

    def test_the_sql_executes(self) -> None:
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute('CREATE SCHEMA "PUBLIC"')
        con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT)')
        con.execute('CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, AMOUNT DOUBLE)')
        con.execute('CREATE TABLE "PUBLIC"."REFUNDS" (DATE_KEY INT, REFUND DOUBLE)')
        con.execute('INSERT INTO "PUBLIC"."DATES" VALUES (1, 3)')
        con.execute('INSERT INTO "PUBLIC"."REFUNDS" VALUES (1, 4.0)')
        rows = con.execute(self._sql()).fetchall()
        # A month with refunds but no sales still reports the declared 0.
        assert rows == [(3, 0, 4.0)], rows


class TestDefaultValueWithDelegatedAggregation:
    """``aggregation: measure`` hands resolution to the engine, so OBSL never
    sees the empty set it would substitute for. Refused rather than accepted
    and dropped, which is what happened: the SQL came out as a bare
    ``MEASURE(...)`` with the declared default nowhere in it."""

    def test_the_combination_is_refused(self) -> None:
        yaml_str = """\
version: 1.0
dataObjects:
  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Region: {code: REGION, abstractType: string}
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
measures:
  Total Revenue:
    resultType: float
    aggregation: measure
    defaultValue: 0
"""
        raw, source_map = TrackedLoader().load_string(yaml_str)
        _, result = ReferenceResolver().resolve(raw, source_map)
        assert not result.valid
        assert "defaultValue" in " ".join(e.message for e in result.errors)


# ---------------------------------------------------------------------------
# One rule, checked against every shape rather than site by site
# ---------------------------------------------------------------------------

SHAPES = """\
version: 1.0

dataObjects:
  Dates:
    code: DATES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int, primaryKey: true}
      Month: {code: MONTH, abstractType: int}
      Day: {code: DAY, abstractType: int}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Amount: {code: AMOUNT, abstractType: float}
      Qty: {code: QTY, abstractType: int}
      Label: {code: LABEL, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

  Refunds:
    code: REFUNDS
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Refund: {code: REFUND, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

dimensions:
  Month: {dataObject: Dates, column: Month, resultType: int}
  Day: {dataObject: Dates, column: Day, resultType: int}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Avg Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: avg
    total: true
  Avg By Month:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: avg
    grain: {mode: FIXED, keepOnly: [Month]}
  Sum By Month:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    grain: {mode: FIXED, keepOnly: [Month]}
  Pair Count:
    columns:
      - {dataObject: Sales, column: Amount}
      - {dataObject: Sales, column: Qty}
    resultType: int
    aggregation: count
    distinct: true
  Sale List:
    columns: [{dataObject: Sales, column: Label}]
    resultType: string
    aggregation: listagg
    delimiter: "|"
    withinGroup:
      column: {dataObject: Sales, column: Amount}
      order: ASC
  Self List:
    columns: [{dataObject: Sales, column: Label}]
    resultType: string
    aggregation: listagg
    delimiter: "|"
    withinGroup:
      column: {dataObject: Sales, column: Label}
      order: ASC
  Refund Amount:
    columns: [{dataObject: Refunds, column: Refund}]
    resultType: float
    aggregation: sum
  Early Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: FIXED

metrics:
  Net Amount:
    expression: "{[Sales Amount]} - {[Refund Amount]}"
  Amount Share:
    expression: "{[Sales Amount]} / {[Avg Amount]}"
  Running Amount:
    type: cumulative
    measure: Sales Amount
    timeDimension: Month
"""

_SHAPE_MEASURES = [
    "Sales Amount",
    "Avg Amount",
    "Avg By Month",
    "Sum By Month",
    "Pair Count",
    "Sale List",
    "Self List",
    "Early Amount",
]

# Every measure the query names, with a default declared on all of them - a
# string one where the measure's own type is a string, since COALESCE takes one
# type and an engine is right to reject a VARCHAR defaulting to 0.
SHAPES_DEFAULTED = SHAPES.replace(
    "    aggregation: ", "    defaultValue: 0\n    aggregation: "
).replace(
    "defaultValue: 0\n    aggregation: listagg", "defaultValue: none\n    aggregation: listagg"
)

_DEFAULT_LITERALS = {"0", "'none'"}

# Two measures produce SQL that does not bind when a second fact puts the query
# on the CFL path — on this branch and on ``main`` alike, with and without a
# declared default. ``filter_wrap`` and the metric branch of ``total_wrap`` both
# rebuild the aggregate from the measure's resolved expression, which names a
# fact table the CFL plan replaced with the composite CTE. That is the same
# mistake this file is about, in a place the default has nothing to do with, so
# it is recorded here rather than fixed in a change about ``defaultValue``.
UNBOUND_IN_CFL = {"Early Amount", "Amount Share"}


def _without_defaults(sql: str) -> str:
    """*sql* as a canonical tree with each declared default's COALESCE removed.

    Compared as a tree rather than as text so that the parentheses the default
    brought with it do not count as a difference — ``x / COALESCE(a / b, 0)``
    and ``x / (a / b)`` are the same expression, and ``x / a / b`` is not.
    Nothing else in these queries writes a two-argument COALESCE over one of
    the declared defaults: CFL's dimension coalesce takes two column refs.
    """
    if not sql.lstrip().upper().startswith(("WITH", "SELECT")):
        return sql  # a refusal, compared verbatim

    def reduce(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Coalesce):
            args = [node.this, *node.expressions]
            if len(args) == 2 and args[1].sql() in _DEFAULT_LITERALS:
                return args[0]
        if isinstance(node, exp.Paren):
            return node.this
        return node

    # Repeated because sqlglot's transform is top-down: a node it replaces is
    # not descended into, so a COALESCE removed on one pass leaves whatever it
    # wrapped untouched until the next.
    tree = sqlglot.parse_one(sql, read="duckdb")
    while (reduced := tree.transform(reduce)) and repr(reduced) != repr(tree):
        tree = reduced
    return repr(tree)


def _compiled(yaml_str: str, measures: list[str], dimensions: list[str]) -> str:
    """The SQL, or the refusal — a refusal has to match between the two too.

    Every query carries a filter so that a measure declaring a ``filterContext``
    has one to ignore, which is what puts it on the ``filter_wrap`` path.
    """
    model = _load(yaml_str)  # loudly, so a broken fixture cannot compare equal
    query = QueryObject(
        select=QuerySelect(dimensions=dimensions, measures=measures),
        where=[QueryFilter(field="Day", op=FilterOperator.GREATER, value=0)],
    )
    try:
        return PIPELINE.compile(query, model, "duckdb").sql
    except Exception as exc:  # noqa: BLE001 - the message is the comparison
        return f"{type(exc).__name__}: {exc}"


class TestTheDefaultOnlyEverAddsACoalesce:
    """``defaultValue`` presents as ``COALESCE(<aggregate>, <default>)``, which
    every pass that *rebuilds* an aggregate has to see through: the window
    helpers a total AVG decomposes into, the union columns CFL spreads a
    multi-argument aggregate across, the sort key an ordered aggregate carries.
    Each read the shape of ``expression`` and found the COALESCE instead, and
    each was found separately, one review round at a time.

    So this asserts the rule rather than the sites: compiling with the default
    declared must produce the same SQL as compiling without it, once the
    ``COALESCE`` wrappers are removed. A pass that mistakes the default for part
    of the aggregate cannot satisfy that, whether or not anyone thought to name
    it here.
    """

    @pytest.mark.parametrize("measure", _SHAPE_MEASURES)
    @pytest.mark.parametrize("dimensions", [["Month"], ["Month", "Day"]])
    def test_single_fact(self, measure: str, dimensions: list[str]) -> None:
        plain = _compiled(SHAPES, [measure], dimensions)
        defaulted = _compiled(SHAPES_DEFAULTED, [measure], dimensions)
        assert _without_defaults(defaulted) == _without_defaults(plain)

    @pytest.mark.parametrize("measure", _SHAPE_MEASURES)
    def test_multi_fact(self, measure: str) -> None:
        """The same measure alongside one the join graph cannot reach from it,
        so the planner unions two legs and rebuilds the aggregate outside."""
        plain = _compiled(SHAPES, [measure, "Refund Amount"], ["Month"])
        defaulted = _compiled(SHAPES_DEFAULTED, [measure, "Refund Amount"], ["Month"])
        assert _without_defaults(defaulted) == _without_defaults(plain)

    @pytest.mark.parametrize("metric", ["Net Amount", "Amount Share", "Running Amount"])
    def test_metric_over_defaulted_components(self, metric: str) -> None:
        """A metric inlines its components' aggregates, so it rebuilds them
        too - a derived one directly, a cumulative one inside its window."""
        plain = _compiled(SHAPES, [metric], ["Month"])
        defaulted = _compiled(SHAPES_DEFAULTED, [metric], ["Month"])
        assert _without_defaults(defaulted) == _without_defaults(plain)

    def test_the_comparison_would_notice(self) -> None:
        """The comparison is only worth something if the default reaches the
        SQL at all, and if re-association still counts as a difference."""
        defaulted = _compiled(SHAPES_DEFAULTED, ["Sales Amount"], ["Month"])
        assert "COALESCE" in defaulted
        assert _without_defaults(defaulted) != _without_defaults(
            defaulted.replace("COALESCE", "COALESCE_")
        )
        assert _without_defaults("SELECT x / (a / b) AS m") != _without_defaults(
            "SELECT x / a / b AS m"
        )

    def test_the_shapes_execute(self) -> None:
        """Equivalence to a baseline is not validity — the baseline could be
        wrong too. ``COALESCE(AVG(x), 0)`` decomposed as it stood produced
        ``SUM(AVG(x), 0)``, which DuckDB rejects outright."""
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute('CREATE SCHEMA "PUBLIC"')
        con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT, DAY INT)')
        con.execute(
            'CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, AMOUNT DOUBLE, QTY INT, LABEL VARCHAR)'
        )
        con.execute('CREATE TABLE "PUBLIC"."REFUNDS" (DATE_KEY INT, REFUND DOUBLE)')
        con.execute('INSERT INTO "PUBLIC"."DATES" VALUES (1, 3, 4), (2, 3, 20)')
        con.execute("INSERT INTO \"PUBLIC\".\"SALES\" VALUES (1, 10.0, 2, 'a'), (2, 20.0, 3, 'b')")
        con.execute('INSERT INTO "PUBLIC"."REFUNDS" VALUES (1, 1.0)')
        for measure in (*_SHAPE_MEASURES, "Net Amount", "Amount Share", "Running Amount"):
            for measures in ([measure], [measure, "Refund Amount"]):
                if len(measures) > 1 and measure in UNBOUND_IN_CFL:
                    continue
                sql = _compiled(SHAPES_DEFAULTED, measures, ["Month"])
                if not sql.lstrip().upper().startswith(("WITH", "SELECT")):
                    continue  # a refusal, and the equivalence test compared it
                con.execute(sql).fetchall()

    def test_a_total_avg_averages_the_rows_not_the_groups(self) -> None:
        """The default comes off the column the window reads and goes back on
        around the window, so it cannot land inside the sum+count helpers."""
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute('CREATE SCHEMA "PUBLIC"')
        con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT, DAY INT)')
        con.execute(
            'CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, AMOUNT DOUBLE, QTY INT, LABEL VARCHAR)'
        )
        con.execute('CREATE TABLE "PUBLIC"."REFUNDS" (DATE_KEY INT, REFUND DOUBLE)')
        con.execute('INSERT INTO "PUBLIC"."DATES" VALUES (1, 1, 4), (2, 2, 4)')
        # Two rows in one month, one in the other: 30/3, not the mean of the
        # two months' means (which would be 12.5).
        con.execute(
            'INSERT INTO "PUBLIC"."SALES" VALUES '
            "(1, 5.0, 1, 'a'), (1, 15.0, 1, 'b'), (2, 10.0, 1, 'c')"
        )
        con.execute('INSERT INTO "PUBLIC"."REFUNDS" VALUES (1, 1.0)')
        rows = con.execute(_compiled(SHAPES_DEFAULTED, ["Avg Amount"], ["Month"])).fetchall()
        assert {r[1] for r in rows} == {10.0}, rows
