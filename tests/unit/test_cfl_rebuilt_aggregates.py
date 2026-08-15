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
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import QueryResolver, ResolutionError
from orionbelt.models.query import QueryObject, QuerySelect
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

metrics:
  Sales Share:
    expression: "{[Sales Amount]} / {[Total Sales]}"
  Sales vs Average:
    expression: "{[Sales Amount]} / {[Avg Sales]}"
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
    """A ``filterContext`` needs its own filtered scan of the fact. The
    composite offers neither: its legs applied the query's filters before it
    existed, and its columns are the projected measures rather than the fact's.
    """

    def test_it_is_refused(self) -> None:
        with pytest.raises(ResolutionError) as exc:
            _sql(["Unfiltered Sales", "Refund Amount"])
        message = str(exc.value)
        assert "Unfiltered Sales" in message
        assert "filterContext" in message

    def test_the_refusal_names_the_plan_as_the_reason(self) -> None:
        with pytest.raises(ResolutionError) as exc:
            _sql(["Unfiltered Sales", "Refund Amount"])
        assert "UNION ALL" in str(exc.value)

    @pytest.mark.parametrize("dialect", ["bigquery", "clickhouse", "postgres", "snowflake"])
    def test_refused_on_every_dialect(self, dialect: str) -> None:
        """The plan is the reason, not the engine — so no dialect quietly
        compiles it."""
        with pytest.raises(ResolutionError):
            _sql(["Unfiltered Sales", "Refund Amount"], dialect)

    def test_still_allowed_on_a_single_fact(self) -> None:
        sql = _sql(["Unfiltered Sales", "Sales Amount"])
        assert "UNION ALL" not in sql
        assert "fc_0" in sql
        assert _run(sql)


class TestTheCompositeFlag:
    """``composite_cte`` says a union was *built*, where ``requires_cfl`` only
    says one was asked for — the CFL planner delegates back to the star planner
    whenever the measures turn out to reach a single leg."""

    def test_set_when_the_plan_unions(self) -> None:
        model = _model()
        query = QueryObject(
            select=QuerySelect(dimensions=["Month"], measures=["Sales Amount", "Refund Amount"])
        )
        resolved = QueryResolver().resolve(query, model)
        assert resolved.requires_cfl
        PIPELINE._cfl_planner.plan(resolved, model, union_by_name=True)
        assert resolved.composite_cte == "composite_01"

    def test_unset_on_a_star_plan(self) -> None:
        model = _model()
        query = QueryObject(select=QuerySelect(dimensions=["Month"], measures=["Sales Amount"]))
        resolved = QueryResolver().resolve(query, model)
        PIPELINE._star_planner.plan(resolved, model)
        assert resolved.composite_cte is None
