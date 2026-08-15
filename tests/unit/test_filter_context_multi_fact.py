"""A filterContext measure in a query that spans facts.

A filterContext is a differently filtered scan of the measure's fact, which
``filter_wrap`` builds as a CTE and joins back on the query's dimensions. Under
a multi-fact plan it had nothing to scan: the measure was planned into a UNION
ALL leg, and the wrapper then tried to re-read the composite - whose legs had
already applied the query's filters and whose columns are the projected
measures rather than the fact's.

The measure never belonged in the union. It reads one fact, at one grain, under
its own filters, and joins back on the dimensions - which is a star query, and
which is what the wrapper now plans it as.
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
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
      Day: {code: DAY, abstractType: int}

  Stores:
    code: STORES
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: STORE_KEY, abstractType: int, primaryKey: true}
      Store Name: {code: STORE_NAME, abstractType: string}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int}
      Store Key: {code: STORE_KEY, abstractType: int}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Stores
        columnsFrom: [Store Key]
        columnsTo: [Store Key]

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
  Store Name: {dataObject: Stores, column: Store Name, resultType: string}

measures:
  Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Refund Amount:
    columns: [{dataObject: Refunds, column: Refund}]
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
  Grand Unfiltered Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    total: true
    filterContext:
      mode: FIXED
  North Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: FIXED
      include:
        - field: Store Name
          op: "="
          value: North
  Unfiltered Refunds:
    columns: [{dataObject: Refunds, column: Refund}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: FIXED

metrics:
  Revenue Share:
    expression: "{[Revenue]} / {[Unfiltered Revenue]}"
"""


def _model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _sql(measures: list[str], dimensions: list[str] | None = None) -> str:
    return PIPELINE.compile(
        QueryObject(
            select=QuerySelect(
                dimensions=["Month"] if dimensions is None else dimensions, measures=measures
            ),
            where=[QueryFilter(field="Day", op=FilterOperator.EQUALS, value=1)],
        ),
        _model(),
        "duckdb",
    ).sql


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute('CREATE SCHEMA "PUBLIC"')
    con.execute('CREATE TABLE "PUBLIC"."DATES" (DATE_KEY INT, MONTH INT, DAY INT)')
    con.execute('CREATE TABLE "PUBLIC"."STORES" (STORE_KEY INT, STORE_NAME VARCHAR)')
    con.execute('CREATE TABLE "PUBLIC"."SALES" (DATE_KEY INT, STORE_KEY INT, AMOUNT DOUBLE)')
    con.execute('CREATE TABLE "PUBLIC"."REFUNDS" (DATE_KEY INT, REFUND DOUBLE)')
    con.execute('INSERT INTO "PUBLIC"."DATES" VALUES (1, 1, 1), (2, 1, 2), (3, 2, 1)')
    con.execute("INSERT INTO \"PUBLIC\".\"STORES\" VALUES (1, 'North'), (2, 'South')")
    # Month 1: 2 on the filtered day, 38 on another. Month 2: 5, all on day 1.
    con.execute('INSERT INTO "PUBLIC"."SALES" VALUES (1, 1, 2.0), (2, 1, 38.0), (3, 2, 5.0)')
    con.execute('INSERT INTO "PUBLIC"."REFUNDS" VALUES (1, 1.0), (3, 4.0)')
    return con


def _keyed(
    measures: list[str], dimensions: list[str] | None = None
) -> dict[tuple, dict[str, float | None]]:
    """Rows as ``{dimension tuple: {measure: value}}``, for queries whose row
    sets differ - a union brings rows one fact alone would not have."""
    dims = ["Month"] if dimensions is None else dimensions
    result = _connect().execute(_sql(measures, dimensions)).fetchdf()
    out: dict[tuple, dict[str, float | None]] = {}
    for row in result.to_dict("records"):
        key = tuple(None if _is_null(row[d]) else row[d] for d in dims)
        out[key] = {name: None if _is_null(row[name]) else float(row[name]) for name in measures}
    return out


def _is_null(value: object) -> bool:
    return value is None or (isinstance(value, float) and value != value)


def _rows(measures: list[str], dimensions: list[str] | None = None) -> dict[str, list[float]]:
    dims = ["Month"] if dimensions is None else dimensions
    result = _connect().execute(_sql(measures, dimensions)).fetchdf()
    if dims:
        result = result.sort_values(dims).reset_index(drop=True)
    return {name: [float(v) for v in result[name]] for name in measures}


_FILTER_CONTEXTS = ["Unfiltered Revenue", "Revenue Any Day", "Grand Unfiltered Revenue"]


class TestTheValueDoesNotDependOnWhatElseIsSelected:
    """The oracle. A filterContext measure reads one fact under its own
    filters; a measure from another fact changes how the *rest* of the query is
    planned and must not change that. Anything that leaves the measure in the
    union, drops its context, or applies the query's filters to it breaks this
    equality without anyone having to predict how."""

    @pytest.mark.parametrize("measure", _FILTER_CONTEXTS)
    def test_alone_and_alongside_another_fact(self, measure: str) -> None:
        assert _rows([measure, "Refund Amount"])[measure] == _rows([measure])[measure]

    @pytest.mark.parametrize("measure", _FILTER_CONTEXTS)
    def test_at_a_grain_only_one_fact_reaches(self, measure: str) -> None:
        """``Store Name`` hangs off Sales, which the refunds leg cannot reach.
        The union adds rows for the refunds under a NULL store, so the row
        *sets* differ - but every row the isolated measure answers on its own
        has to keep its answer here."""
        dims = ["Month", "Store Name"]
        alone = _keyed([measure], dims)
        together = _keyed([measure, "Refund Amount"], dims)
        assert {k: v[measure] for k, v in together.items() if k in alone} == {
            k: v[measure] for k, v in alone.items()
        }
        # ...and the extra rows are the refunds', with no store and no revenue.
        assert all(k[1] is None for k in together.keys() - alone.keys())

    def test_two_contexts_over_two_facts(self) -> None:
        """One isolated measure per fact, in one multi-fact query."""
        together = _rows(["Unfiltered Revenue", "Unfiltered Refunds"])
        assert together["Unfiltered Revenue"] == _rows(["Unfiltered Revenue"])["Unfiltered Revenue"]
        assert together["Unfiltered Refunds"] == _rows(["Unfiltered Refunds"])["Unfiltered Refunds"]

    def test_an_include_filter_gets_its_own_join(self) -> None:
        """``include:`` adds a filter of the context's own, on a column no
        other part of the query reads. The scan is resolved for itself, so it
        joins what that filter needs - North's sales are 40 of month 1's, and
        month 2 has no North store at all."""
        for measures in (
            ["North Revenue"],
            ["North Revenue", "Refund Amount"],
            ["Revenue", "North Revenue", "Refund Amount"],
        ):
            rows = _keyed(measures)
            assert rows[(1,)]["North Revenue"] == 40.0
            assert rows[(2,)]["North Revenue"] is None
        assert '"PUBLIC"."STORES"' in _sql(["Revenue", "North Revenue", "Refund Amount"])

    def test_a_metric_over_it_too(self) -> None:
        direct = _rows(["Revenue", "Unfiltered Revenue", "Refund Amount"])
        share = _rows(["Revenue Share", "Refund Amount"])["Revenue Share"]
        expected = [
            n / d for n, d in zip(direct["Revenue"], direct["Unfiltered Revenue"], strict=True)
        ]
        assert [round(v, 6) for v in share] == [round(v, 6) for v in expected]

    def test_the_context_is_doing_something(self) -> None:
        """The equalities above are only worth something if the context changes
        the number: month 1 holds 2 on the filtered day out of 40."""
        assert _rows(["Revenue", "Unfiltered Revenue", "Refund Amount"]) == {
            "Revenue": [2.0, 5.0],
            "Unfiltered Revenue": [40.0, 5.0],
            "Refund Amount": [1.0, 4.0],
        }


# Two facts in the union, plus the isolated measure - the shape the wrapper has
# to keep out of it.
_MULTI_FACT = ["Revenue", "Refund Amount", "Unfiltered Revenue"]


class TestTheShapeOfThePlan:
    def test_the_plan_is_still_multi_fact(self) -> None:
        assert "UNION ALL" in _sql(_MULTI_FACT)

    def test_the_isolated_fact_still_earns_a_leg(self) -> None:
        """Its measure is gone from the union but its fact is not. The union is
        what makes a dimension only that fact reaches available at all,
        NULL-padded in the others, so dropping the leg would leave the query
        unable to group by one."""
        sql = _sql(["Unfiltered Revenue", "Refund Amount"], ["Month", "Store Name"])
        assert "UNION ALL" in sql
        composite = sql.split('"fc_0" AS')[0]
        assert '"Stores"."STORE_NAME"' in composite
        assert '"Unfiltered Revenue"' not in composite

    def test_the_isolated_measure_is_not_in_the_union(self) -> None:
        sql = _sql(_MULTI_FACT)
        composite = sql.split('"fc_0" AS')[0]
        assert '"Unfiltered Revenue"' not in composite

    def test_the_isolated_scan_reads_the_fact(self) -> None:
        sql = _sql(_MULTI_FACT)
        fc_block = sql.split('"fc_0" AS')[1].split("\n)")[0]
        assert '"PUBLIC"."SALES"' in fc_block
        assert "composite_01" not in fc_block

    def test_that_scan_does_not_carry_the_query_filter(self) -> None:
        sql = _sql(_MULTI_FACT)
        fc_block = sql.split('"fc_0" AS')[1].split("\n)")[0]
        assert '"Dates"."DAY" = 1' not in fc_block

    def test_the_composite_is_unchanged_by_its_presence(self) -> None:
        """Adding an isolated measure must not alter how the rest is planned."""
        with_fc = _sql(["Revenue", "Refund Amount", "Unfiltered Revenue"])
        without = _sql(["Revenue", "Refund Amount"])
        assert (
            with_fc.split('"fc_0" AS')[0]
            .split('"composite_01" AS')[1]
            .startswith(without.split('"composite_01" AS')[1].split("\n)")[0])
        )

    def test_a_single_fact_query_is_unchanged(self) -> None:
        sql = _sql(["Revenue", "Unfiltered Revenue"])
        assert "UNION ALL" not in sql
        assert '"main" AS' in sql and '"fc_0" AS' in sql


class TestWhatIsStillRefused:
    def test_a_grain_the_isolated_fact_cannot_reach(self) -> None:
        """``fc_N`` groups by the dimensions it joins back on, so an isolated
        measure on Refunds cannot be asked for by store - refunds have none.
        Refused by the reachability rule that already governs this, naming the
        object and what to do about it, rather than by a rule of its own."""
        with pytest.raises(ResolutionError) as exc:
            _sql(["Unfiltered Refunds", "Revenue"], ["Month", "Store Name"])
        message = str(exc.value)
        assert "'Stores'" in message
        assert "queried independently" in message
