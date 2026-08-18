"""What a post-planning pass may rebuild a measure's aggregate from.

Most wrappers pass a measure's column through by name, but several re-project
its *aggregate* into a CTE of their own: ``total_wrap`` decomposes a metric's
components, ``window_wrap`` and ``cumulative_wrap`` rebuild a base measure. Each
used the measure's resolved expression for that, which names the fact table the
measure was resolved against - true under a star plan, whose CTE reuses the
planner's own FROM, and false under a multi-fact one, whose CTE selects from the
UNION ALL composite and has no fact table in scope at all.

The plan now records what it projects each measure as
(``ResolvedQuery.projected_expressions``), so a pass rebuilds from that rather
than from the resolved expression. The one thing that cannot be rebuilt is a
``filterContext``, and that is refused rather than mis-compiled.
"""

from __future__ import annotations

import duckdb

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import QueryResolver
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
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
  Total Sales:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    total: true
  Avg Sales:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: avg
    total: true
  Refund Amount:
    columns: [{dataObject: Refunds, column: Refund}]
    resultType: float
    aggregation: sum
  Unfiltered Sales:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: FIXED
  Grand Unfiltered Sales:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    grain:
      mode: FIXED
    filterContext:
      mode: FIXED

metrics:
  Sales Share:
    expression: "{[Sales Amount]} / {[Total Sales]}"
  Sales vs Average:
    expression: "{[Sales Amount]} / {[Avg Sales]}"
  Filtered Share:
    expression: "{[Sales Amount]} / {[Unfiltered Sales]}"
"""


def _model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _sql(measures: list[str], dialect: str = "duckdb") -> str:
    return PIPELINE.compile(
        QueryObject(select=QuerySelect(dimensions=["Month"], measures=measures)),
        _model(),
        dialect,
    ).sql


def _run(sql: str) -> list[tuple]:
    con = duckdb.connect(":memory:")
    con.execute('CREATE SCHEMA "PUBLIC"')
    con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT)')
    con.execute('CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, AMOUNT DOUBLE)')
    con.execute('CREATE TABLE "PUBLIC"."REFUNDS" (DATE_KEY INT, REFUND DOUBLE)')
    con.execute('INSERT INTO "PUBLIC"."DATES" VALUES (1, 1), (2, 2)')
    # 30 across two months, so a share of the grand total is a round number.
    con.execute('INSERT INTO "PUBLIC"."SALES" VALUES (1, 10.0), (2, 15.0), (2, 5.0)')
    con.execute('INSERT INTO "PUBLIC"."REFUNDS" VALUES (1, 1.0)')
    return con.execute(sql).fetchall()


class TestAMetricOverATotalComponent:
    """``total_wrap`` splits a metric into its components so it can put a window
    over the ones carrying ``total``. Under CFL those components have to be
    re-aggregated from the composite CTE, not from the fact the resolved
    expression names."""

    def test_the_plan_is_multi_fact(self) -> None:
        assert "UNION ALL" in _sql(["Sales Share", "Refund Amount"])

    def test_components_read_the_composite(self) -> None:
        sql = _sql(["Sales Share", "Refund Amount"])
        assert 'SUM("composite_01"."Sales Amount")' in sql
        assert '"Sales"."AMOUNT"' not in sql.split("UNION ALL")[-1]

    def test_it_binds_and_answers(self) -> None:
        rows = sorted(_run(_sql(["Sales Share", "Refund Amount"])))
        # Month 1 is 10 of 30, month 2 is 20 of 30.
        assert [(r[0], round(r[1], 4)) for r in rows] == [(1, 0.3333), (2, 0.6667)]

    def test_an_avg_component_decomposes_over_the_composite(self) -> None:
        """A total AVG becomes SUM and COUNT helpers, whose argument is the
        column the plan projects — the composite's, not the fact's."""
        sql = _sql(["Sales vs Average", "Refund Amount"])
        assert 'SUM("composite_01"."Avg Sales")' in sql
        assert 'COUNT("composite_01"."Avg Sales")' in sql

    def test_the_avg_metric_binds_and_answers(self) -> None:
        rows = sorted(_run(_sql(["Sales vs Average", "Refund Amount"])))
        # The average is over rows (30/3 = 10), not over months.
        assert [(r[0], round(r[1], 4)) for r in rows] == [(1, 1.0), (2, 2.0)]

    def test_a_direct_total_avg_too(self) -> None:
        """Not only through a metric: a total AVG selected directly was
        decomposing the same way."""
        sql = _sql(["Avg Sales", "Refund Amount"])
        assert 'SUM("composite_01"."Avg Sales")' in sql
        assert {round(r[1], 4) for r in _run(sql)} == {10.0}

    def test_a_single_fact_plan_is_unchanged(self) -> None:
        """The star plan's CTE reuses the planner's FROM, so there the resolved
        expression is exactly right and still what gets projected."""
        sql = _sql(["Sales Share"])
        assert "UNION ALL" not in sql
        assert 'SUM("Sales"."AMOUNT")' in sql


class TestFilterContextInAMultiFactPlan:
    """A filterContext is a differently filtered scan of the measure's fact.
    The composite offers nothing to scan - its legs applied the query's filters
    before it existed - so the measure is kept out of the union and planned as
    the star query it is. See ``test_filter_context_multi_fact`` for what that
    plan looks like and what it answers."""

    def test_it_compiles(self) -> None:
        sql = _sql(["Unfiltered Sales", "Refund Amount"])
        assert '"fc_0" AS' in sql

    def test_the_measure_is_not_in_the_composite(self) -> None:
        sql = _sql(["Unfiltered Sales", "Sales Amount", "Refund Amount"])
        assert '"Unfiltered Sales"' not in sql.split('"fc_0" AS')[0]

    def test_it_answers_what_it_does_on_its_own(self) -> None:
        """The isolated measure is projected last either way, after whatever
        the rest of the query brought."""
        together = _run(_sql(["Unfiltered Sales", "Refund Amount"]))
        alone = _run(_sql(["Unfiltered Sales"]))
        assert [float(r[-1]) for r in together] == [float(r[-1]) for r in alone]

    def test_a_single_fact_query_still_works(self) -> None:
        sql = _sql(["Unfiltered Sales", "Sales Amount"])
        assert "UNION ALL" not in sql
        assert "fc_0" in sql
        assert _run(sql)


class TestFilterContextReadThroughAMetric:
    """A metric inlines its components' aggregates into one column, so a
    context declared on a component used to be dropped silently. The component
    now gets the same CTE a selected measure gets, and the formula is rebuilt
    over the CTEs' columns - see ``test_filter_context_metrics``. Under a
    multi-fact plan that CTE has the same nothing to read as a selected one, so
    the refusal covers components too.
    """

    @staticmethod
    def _sql_filtered(measures: list[str]) -> str:
        return PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Month"], measures=measures),
                where=[QueryFilter(field="Month", op=FilterOperator.EQUALS, value=1)],
            ),
            _model(),
            "duckdb",
        ).sql

    def test_it_compiles_on_a_single_fact_plan(self) -> None:
        sql = self._sql_filtered(["Filtered Share"])
        assert '"fc_0" AS' in sql
        assert '"main"."Sales Amount" / NULLIF("fc_0"."Unfiltered Sales", 0)' in sql

    def test_it_compiles_alongside_another_fact(self) -> None:
        """The component is kept out of the union like a selected one, and the
        metric is rebuilt over the CTEs the wrapper leaves behind."""
        sql = self._sql_filtered(["Filtered Share", "Refund Amount"])
        assert '"fc_0" AS' in sql
        assert '"Unfiltered Sales"' not in sql.split('"fc_0" AS')[0]

    def test_the_metric_answers_the_same_either_way(self) -> None:
        together = _run(self._sql_filtered(["Filtered Share", "Refund Amount"]))
        alone = _run(self._sql_filtered(["Filtered Share"]))
        assert [round(float(r[1]), 6) for r in together] == [round(float(r[1]), 6) for r in alone]

    def test_the_component_still_works_when_selected_directly(self) -> None:
        """Selecting it by name gets its own filtered scan, with the query's
        WHERE left out of it."""
        sql = self._sql_filtered(["Grand Unfiltered Sales", "Sales Amount"])
        assert "fc_0" in sql
        # The wrapper projects the inline measure first, then the isolated one.
        rows = [(float(r[1]), float(r[2])) for r in _run(sql)]
        # Month 1 holds 10 of the 30, and the unfiltered measure reports all 30
        # even though the query asked only for month 1.
        assert rows == [(10.0, 30.0)]

    def test_a_metric_over_plain_components_is_untouched(self) -> None:
        assert "Sales Share" in self._sql_filtered(["Sales Share"])


class TestTheFilterContextFlagCoversComponents:
    """``has_filter_context`` decides whether the pass runs at all, and it read
    only the directly selected measures while its siblings (``has_totals``,
    ``has_grain_overrides``) walk metric components. That gap is what let the
    metric case through silently."""

    @staticmethod
    def _resolved(measures: list[str]):
        return QueryResolver().resolve(
            QueryObject(select=QuerySelect(dimensions=["Month"], measures=measures)),
            _model(),
        )

    def test_true_for_a_directly_selected_measure(self) -> None:
        assert self._resolved(["Unfiltered Sales"]).has_filter_context

    def test_true_for_a_component_read_through_a_metric(self) -> None:
        assert self._resolved(["Filtered Share"]).has_filter_context

    def test_false_without_one(self) -> None:
        assert not self._resolved(["Sales Amount"]).has_filter_context
