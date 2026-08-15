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

metrics:
  Revenue Share:
    expression: "{[Revenue]} / {[Unfiltered Revenue]}"
  Day Share:
    expression: "{[Revenue]} / {[Revenue Any Day]}"
  Two Contexts:
    expression: "{[Unfiltered Revenue]} / {[Revenue Any Day]}"
  Plain Ratio:
    expression: "{[Revenue]} / {[Sale Count]}"
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
    con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT, DAY INT)')
    con.execute('CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, REGION VARCHAR, AMOUNT DOUBLE)')
    # Month 1 has 10 on day 1 and 30 on day 2; month 2 has 5 on day 1.
    con.execute('INSERT INTO "PUBLIC"."DATES" VALUES (1, 1, 1), (2, 1, 2), (3, 2, 1)')
    con.execute(
        "INSERT INTO \"PUBLIC\".\"SALES\" VALUES (1, 'EU', 10.0), (2, 'EU', 30.0), (3, 'US', 5.0)"
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
        Month 1 holds 10 on the filtered day out of 40 altogether; month 2's
        one row is on that day, so there the context changes nothing."""
        assert _rows(["Revenue", "Unfiltered Revenue"], ["Month"]) == {
            "Revenue": [10.0, 5.0],
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
        assert [round(v, 6) for v in rows["Revenue Share"]] == [0.25, 1.0]


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
