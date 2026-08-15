"""A metric reading a measure that declares a ``filterContext``.

``filterContext`` says which of the query's WHERE filters apply to a measure,
and a different WHERE needs a scan of its own - ``filter_wrap`` gives a
*selected* measure exactly that, in a CTE joined back on the query dimensions.

A metric is one column, though, and the planner builds it by substituting each
component's aggregate into the formula. Both halves of ``{[Revenue]} /
{[Unfiltered Revenue]}`` therefore landed in the same SELECT under the same
WHERE, the context was dropped, and the ratio came back 1. The component is now
computed in the same CTE a selected measure would get, and the formula is
rebuilt in the outer query out of CTE columns - the move ``grain_dedup`` makes
for a deduplicated component and ``total_wrap`` for a windowed one.
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

PIPELINE = CompilationPipeline()

MODEL_YAML = """\
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
      Order Date: {code: ODATE, abstractType: date}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Region: {code: REGION, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]

dimensions:
  Month: {dataObject: Dates, column: Month, resultType: int}
  Day: {dataObject: Dates, column: Day, resultType: int}
  Region: {dataObject: Sales, column: Region, resultType: string}
  Order Date: {dataObject: Dates, column: Order Date, resultType: date}

measures:
  Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Unfiltered Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: FIXED
  Revenue Any Day:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: RELATIVE
      exclude: [Day]
  Sale Count:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: int
    aggregation: count
  Grand Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    total: true
  Month Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    grain: {mode: FIXED, keepOnly: [Month]}
  Grand Avg:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: avg
    total: true
  Unfiltered Total Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    total: true
    filterContext:
      mode: FIXED

metrics:
  Revenue Share:
    expression: "{[Revenue]} / {[Unfiltered Revenue]}"
  Day Share:
    expression: "{[Revenue]} / {[Revenue Any Day]}"
  Two Contexts:
    expression: "{[Unfiltered Revenue]} / {[Revenue Any Day]}"
  Plain Ratio:
    expression: "{[Revenue]} / {[Sale Count]}"
  Total Share:
    expression: "{[Revenue]} / {[Unfiltered Total Revenue]}"
  Rank Unfiltered:
    type: window
    windowFunction: rank
    measure: Unfiltered Revenue
  Doubled Rank:
    expression: "{[Rank Unfiltered]} * 2"
  Share Of Grand:
    expression: "{[Unfiltered Revenue]} / {[Grand Revenue]}"
  Share Of Month:
    expression: "{[Unfiltered Revenue]} / {[Month Revenue]}"
  Share Of Grand Avg:
    expression: "{[Unfiltered Revenue]} / {[Grand Avg]}"
  Unfiltered YoY:
    type: period_over_period
    expression: "{[Unfiltered Revenue]}"
    periodOverPeriod:
      timeDimension: Order Date
      grain: year
      offset: -1
      offsetGrain: year
      comparison: previousValue
  Revenue YoY:
    type: period_over_period
    expression: "{[Revenue]}"
    periodOverPeriod:
      timeDimension: Order Date
      grain: year
      offset: -1
      offsetGrain: year
      comparison: previousValue
"""


def _model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _dims(dimensions: list[str] | None) -> list[str]:
    """``None`` means the default grain; ``[]`` means no dimensions at all."""
    return ["Month"] if dimensions is None else dimensions


def _query(measures: list[str], dimensions: list[str] | None = None) -> QueryObject:
    """Every query filters on Day, which is the filter the contexts act on."""
    return QueryObject(
        select=QuerySelect(dimensions=_dims(dimensions), measures=measures),
        where=[QueryFilter(field="Day", op=FilterOperator.EQUALS, value=1)],
    )


def _sql(measures: list[str], dimensions: list[str] | None = None) -> str:
    return PIPELINE.compile(_query(measures, dimensions), _model(), "duckdb").sql


def _rows(measures: list[str], dimensions: list[str] | None = None) -> dict[str, list]:
    """Execute, returning one list of values per selected measure."""
    con = duckdb.connect(":memory:")
    con.execute('CREATE SCHEMA "PUBLIC"')
    con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT, DAY INT, ODATE DATE)')
    con.execute('CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, REGION VARCHAR, AMOUNT DOUBLE)')
    # Month 1 has 2 on day 1 and 38 on day 2; month 2 has 5 on day 1. So month 1
    # is the larger month overall and the smaller one on the filtered day —
    # which is what lets a rank tell a dropped context from an honoured one.
    con.execute(
        'INSERT INTO "PUBLIC"."DATES" VALUES '
        "(1, 1, 1, DATE '2024-01-01'), (2, 1, 2, DATE '2024-01-02'), (3, 2, 1, DATE '2024-02-01')"
    )
    con.execute(
        "INSERT INTO \"PUBLIC\".\"SALES\" VALUES (1, 'EU', 2.0), (2, 'EU', 38.0), (3, 'US', 5.0)"
    )
    result = con.execute(_sql(measures, dimensions)).fetchdf()
    # Sorted on the dimensions: nothing orders the rows, and two queries have to
    # be comparable row for row.
    if _dims(dimensions):
        result = result.sort_values(_dims(dimensions)).reset_index(drop=True)
    return {name: [float(v) for v in result[name]] for name in measures}


class TestTheContextSurvivesTheMetric:
    """The oracle: a component's value inside a metric has to be the value the
    same measure reports when selected on its own. Anything that drops the
    context, or applies it twice, breaks that equality without anyone having to
    predict which."""

    @pytest.mark.parametrize(
        ("metric", "numerator", "denominator"),
        [
            ("Revenue Share", "Revenue", "Unfiltered Revenue"),
            ("Day Share", "Revenue", "Revenue Any Day"),
            ("Two Contexts", "Unfiltered Revenue", "Revenue Any Day"),
            ("Plain Ratio", "Revenue", "Sale Count"),
            ("Total Share", "Revenue", "Unfiltered Total Revenue"),
        ],
    )
    def test_the_metric_equals_its_components(
        self, metric: str, numerator: str, denominator: str
    ) -> None:
        direct = _rows([numerator, denominator])
        computed = _rows([metric])[metric]
        expected = [n / d for n, d in zip(direct[numerator], direct[denominator], strict=True)]
        assert [round(v, 6) for v in computed] == [round(v, 6) for v in expected]

    def test_the_context_actually_changes_the_number(self) -> None:
        """The equality above is only worth something if the two sides differ.
        Month 1 holds 2 on the filtered day out of 40 altogether; month 2's one
        row is on that day, so there the context changes nothing."""
        assert _rows(["Revenue", "Unfiltered Revenue"], ["Month"]) == {
            "Revenue": [2.0, 5.0],
            "Unfiltered Revenue": [40.0, 5.0],
        }

    def test_it_holds_at_a_second_grain(self) -> None:
        direct = _rows(["Revenue", "Unfiltered Revenue"], ["Month", "Region"])
        share = _rows(["Revenue Share"], ["Month", "Region"])["Revenue Share"]
        expected = [
            n / d for n, d in zip(direct["Revenue"], direct["Unfiltered Revenue"], strict=True)
        ]
        assert [round(v, 6) for v in share] == [round(v, 6) for v in expected]


class TestTheShapeOfTheSQL:
    def test_the_component_gets_its_own_cte(self) -> None:
        sql = _sql(["Revenue Share"])
        assert '"fc_0" AS' in sql

    def test_that_cte_does_not_carry_the_query_filter(self) -> None:
        sql = _sql(["Revenue Share"])
        fc_block = sql.split('"fc_0" AS')[1]
        assert '"Dates"."DAY" = 1' not in fc_block

    def test_the_formula_reads_columns_not_aggregates(self) -> None:
        """Rebuilt in the outer query out of the two CTEs' columns, rather than
        inlined as two aggregates over one filtered scan."""
        sql = _sql(["Revenue Share"])
        outer = sql.rsplit("\nSELECT ", 1)[1]
        assert '"main"."Revenue" / "fc_0"."Unfiltered Revenue"' in outer
        assert "SUM(" not in outer

    def test_two_contexts_get_two_ctes(self) -> None:
        sql = _sql(["Two Contexts"])
        assert '"fc_0" AS' in sql and '"fc_1" AS' in sql

    def test_a_metric_over_plain_components_is_untouched(self) -> None:
        """No filterContext anywhere in it, so no CTE and no rebuild."""
        sql = _sql(["Plain Ratio"])
        assert "fc_0" not in sql
        assert "SUM(" in sql

    def test_the_component_is_projected_once_when_also_selected(self) -> None:
        """Selecting the measure *and* a metric over it aggregates it once —
        the outer query reads one CTE column twice."""
        sql = _sql(["Revenue Share", "Unfiltered Revenue"])
        assert sql.count('"fc_1" AS') == 0
        assert sql.count('SUM("Sales"."AMOUNT") AS "Unfiltered Revenue"') == 1
        # ...and read twice: once by the metric, once as its own column.
        assert sql.count('"fc_0"."Unfiltered Revenue"') == 2

    def test_both_read_the_same_column(self) -> None:
        rows = _rows(["Revenue Share", "Unfiltered Revenue"])
        assert rows["Unfiltered Revenue"] == [40.0, 5.0]
        assert [round(v, 6) for v in rows["Revenue Share"]] == [0.05, 1.0]


class TestTheEdgesTheRebuildReaches:
    """Three things the wrapper got wrong, two of them reachable only once a
    metric could put a filterContext measure in play."""

    def test_a_scalar_metric_returns_one_row(self) -> None:
        """No dimensions and every component isolated leaves the ``main`` CTE
        with nothing to project — it degenerated to ``SELECT *``, one row per
        fact row, and the CROSS JOIN multiplied the scalar result by all of
        them. It is dropped instead, and an isolated CTE anchors the query."""
        sql = _sql(["Two Contexts"], [])
        assert '"main" AS' not in sql
        assert _rows(["Two Contexts"], [])["Two Contexts"] == [1.0]

    def test_ordering_by_a_rebuilt_metric(self) -> None:
        """The metric is assembled in the outer projection and has no CTE
        column, so it orders by its own select alias."""
        query = _query(["Revenue Share"])
        query.order_by = [QueryOrderBy(field="Revenue Share", direction="desc")]
        sql = PIPELINE.compile(query, _model(), "duckdb").sql
        assert 'ORDER BY "Revenue Share" DESC' in sql

    def test_ordering_by_an_isolated_measure(self) -> None:
        """It orders by the CTE that computed it. Ordering by ``main`` — which
        is where every measure used to be looked up — named a column that CTE
        does not have, and the engine rejected the query outright."""
        query = _query(["Unfiltered Revenue"])
        query.order_by = [QueryOrderBy(field="Unfiltered Revenue", direction="desc")]
        sql = PIPELINE.compile(query, _model(), "duckdb").sql
        assert '"fc_0"."Unfiltered Revenue" DESC' in sql
        assert '"main"."Unfiltered Revenue"' not in sql

    @pytest.mark.parametrize("measure", ["Unfiltered Revenue", "Revenue Share"])
    def test_having_on_a_filter_contexted_value_is_refused(self, measure: str) -> None:
        """HAVING is evaluated inside the CTE the planner built, where the
        measure's own value does not exist — the query-filtered aggregate stood
        in for it and the wrong groups survived, without any sign of it."""
        query = _query([measure])
        query.having = [QueryFilter(field=measure, op=FilterOperator.GREATER, value=10)]
        with pytest.raises(ResolutionError) as exc:
            PIPELINE.compile(query, _model(), "duckdb")
        assert measure in str(exc.value)

    def test_having_on_a_plain_measure_still_works(self) -> None:
        query = _query(["Revenue", "Unfiltered Revenue"])
        query.having = [QueryFilter(field="Revenue", op=FilterOperator.GREATER, value=6)]
        sql = PIPELINE.compile(query, _model(), "duckdb").sql
        assert "HAVING" in sql


class TestAContextOnATotalMeasure:
    """``total: true`` and ``grain: {mode: FIXED}`` make the same claim - a
    grand total - but only the grain override reaches ``effective_grain``. The
    isolated CTE read the query grain instead and came back per group, and
    ``total_wrap`` was never going to correct it: it skips filter-contexted
    measures on the grounds that this wrapper owns them."""

    def test_the_isolated_cte_has_no_grain(self) -> None:
        sql = _sql(["Unfiltered Total Revenue"])
        assert "CROSS JOIN" in sql
        fc_block = sql.split('"fc_0" AS')[1].split("\n)")[0]
        assert '"Month"' not in fc_block

    def test_it_is_the_same_value_on_every_row(self) -> None:
        """Sales total 45 across both months and both days; the query asks for
        one day of one month, and this measure reports all of it regardless."""
        assert _rows(["Unfiltered Total Revenue"])["Unfiltered Total Revenue"] == [45.0, 45.0]

    def test_the_total_is_what_distinguishes_it(self) -> None:
        """Without ``total`` the same context reports per month, so the two
        measures must not agree — otherwise the assertion above proves
        nothing."""
        assert _rows(["Unfiltered Revenue"])["Unfiltered Revenue"] == [40.0, 5.0]


class TestAContextBehindAWindowMetric:
    """``metric_leaf_components`` stops at a cumulative / window /
    period-over-period metric, because that one is computed by its own wrapper
    rather than substituted into the formula. Its *base measure* is still a
    measure the query computes, and a context declared on it is still one to
    honour - a derived metric over a window metric over a filter-contexted
    measure hid one exactly there."""

    def test_a_window_metric_over_it_honours_the_context(self) -> None:
        sql = _sql(["Rank Unfiltered"])
        assert '"fc_0" AS' in sql
        # Unfiltered: month 1 has 110, month 2 has 50. Filtered it is the other
        # way round, so a dropped context would swap the ranks.
        assert _rows(["Rank Unfiltered"])["Rank Unfiltered"] == [1.0, 2.0]

    def test_a_derived_metric_over_that_window_is_refused(self) -> None:
        """The derived expression is a placeholder until the window pass
        resolves it, and this wrapper's CTE would materialize it first. The
        rule that says so could not see the context to fire on."""
        with pytest.raises(ResolutionError) as exc:
            _sql(["Doubled Rank"])
        message = str(exc.value)
        assert "Doubled Rank" in message
        assert "window metric" in message

    def test_the_flag_sees_it(self) -> None:
        from orionbelt.compiler.resolution import QueryResolver

        resolved = QueryResolver().resolve(_query(["Rank Unfiltered"]), _model())
        assert resolved.has_filter_context


class TestAMetricMixingAContextWithATotal:
    """This wrapper runs before ``total_wrap`` and rewrites the FROM, so the
    components it leaves behind are columns of ``main`` and of the isolated
    CTEs. ``total_wrap`` decomposes the same metric next and was rebuilding
    those components from their resolved expressions - naming a fact table its
    own CTE no longer selects from. It reads what this wrapper projected
    instead, recorded in ``projected_expressions`` the way the planners record
    theirs."""

    def test_a_total_component_composes(self) -> None:
        sql = _sql(["Share Of Grand"])
        assert '"Sales"."AMOUNT"' not in sql.split('"base" AS')[-1]
        assert 'SUM("Grand Revenue") OVER ()' in sql

    def test_it_answers_what_the_two_measures_do(self) -> None:
        direct = _rows(["Unfiltered Revenue", "Grand Revenue"])
        share = _rows(["Share Of Grand"])["Share Of Grand"]
        expected = [
            n / d
            for n, d in zip(direct["Unfiltered Revenue"], direct["Grand Revenue"], strict=True)
        ]
        assert [round(v, 6) for v in share] == [round(v, 6) for v in expected]

    def test_a_grain_override_component_composes(self) -> None:
        direct = _rows(["Unfiltered Revenue", "Month Revenue"])
        share = _rows(["Share Of Month"])["Share Of Month"]
        expected = [
            n / d
            for n, d in zip(direct["Unfiltered Revenue"], direct["Month Revenue"], strict=True)
        ]
        assert [round(v, 6) for v in share] == [round(v, 6) for v in expected]

    @pytest.mark.parametrize(
        "measures",
        [
            ["Share Of Grand Avg"],
            ["Unfiltered Revenue", "Grand Avg"],
        ],
        ids=["through a metric", "side by side"],
    )
    def test_an_averaged_total_is_refused(self, measures: list[str]) -> None:
        """An AVG total decomposes into SUM and COUNT of the aggregate's
        *argument*, and by the time this wrapper has run the argument is a
        value that has already been averaged. It was emitting ``SUM(1)`` and
        ``COUNT(1)`` - neither the right number nor valid SQL - and it did so
        whether the two met inside a metric or merely in the same query."""
        with pytest.raises(ResolutionError) as exc:
            _sql(measures)
        message = str(exc.value)
        assert "Grand Avg" in message
        assert "average" in message

    def test_an_averaged_total_without_a_context_is_untouched(self) -> None:
        """The refusal is about what filterContext does to the plan, so a query
        without one still gets its averaged total."""
        assert _rows(["Grand Avg"])["Grand Avg"] == [3.5, 3.5]


class TestAContextUnderAPeriodOverPeriodMetric:
    """A filterContext *is* a differently filtered scan of the fact, held in a
    CTE of its own. Period-over-period rebuilds the query's FROM from a date
    spine, which cannot read that CTE - it was being built and then ignored, so
    the comparison ran on the query-filtered value under the filter-contexted
    measure's name. Refused, the same treatment an anchored measure already
    gets for the same reason."""

    def test_a_pop_metric_over_such_a_measure_is_refused(self) -> None:
        with pytest.raises(ResolutionError) as exc:
            _sql(["Unfiltered YoY"], ["Order Date"])
        message = str(exc.value)
        assert "Unfiltered Revenue" in message
        assert "date spine" in message

    def test_an_unrelated_one_in_the_same_query_is_refused_too(self) -> None:
        """The pass regroups the whole query to its own grain, so a
        filterContext measure that the metric does not read comes back at that
        grain rather than the query's."""
        with pytest.raises(ResolutionError) as exc:
            _sql(["Revenue YoY", "Unfiltered Revenue"], ["Order Date"])
        assert "Unfiltered Revenue" in str(exc.value)

    def test_a_pop_metric_without_one_still_compiles(self) -> None:
        assert "Revenue YoY" in _sql(["Revenue YoY"], ["Order Date"])
