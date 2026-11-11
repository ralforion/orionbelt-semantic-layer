"""Tests for query-level coalesce dimensions.

Covers resolution validation, CFL outer-wrapper SQL emission, and
type-mismatch / alias-collision error paths.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import QueryResolver, ResolutionError
from orionbelt.models.query import (
    CoalesceDimension,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL_PATH = Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml"


def _load_model():
    raw, src = TrackedLoader().load(_MODEL_PATH)
    model, _ = ReferenceResolver().resolve(raw, src)
    return model


# B's measure makes the query CFL, but needs no leg of its own (a filterContext,
# an empty grain): the planner falls back to a star, which returns X and Y.
STAR_FALLBACK_MODEL = """\
version: 1.0
dataObjects:
  A:
    code: a
    columns:
      X: {code: x, abstractType: string}
      Y: {code: y, abstractType: string}
      Amount: {code: amount, abstractType: int}
  B:
    code: b
    columns:
      Amount: {code: amount, abstractType: int}
dimensions:
  X: {dataObject: A, column: X, resultType: string}
  Y: {dataObject: A, column: Y, resultType: string}
measures:
  Revenue: {columns: [{dataObject: A, column: Amount}], aggregation: sum}
  Spend:
    columns: [{dataObject: B, column: Amount}]
    aggregation: sum
    filterContext: {mode: FIXED}
    grain: {mode: FIXED, include: []}
"""


class TestCoalesceCompilation:
    def test_coalesce_emits_in_outer_select_and_group_by(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Sales Employee", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Quantity"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        sql = result.sql

        # Outer wrapper coalesces the two role-playing dims into one alias
        assert 'COALESCE("Sales Employee", "Purchase Employee") AS "Employee"' in sql
        # GROUP BY uses the same expression (portable across dialects)
        assert 'GROUP BY COALESCE("Sales Employee", "Purchase Employee")' in sql

    def test_order_by_coalesce_alias(self) -> None:
        # ORDER BY by the coalesce alias must work — most dialects accept
        # ordering by an alias from the SELECT.
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Sales Employee", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Quantity"],
            ),
            order_by=[QueryOrderBy(field="Employee", direction=SortDirection.ASC)],
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert 'ORDER BY "Employee"' in result.sql

    @pytest.mark.parametrize(
        ("extra", "order_by"),
        [
            ({"limit": 2}, 'ORDER BY "Employee" ASC'),
            ({"grouping": "rollup"}, 'ORDER BY "Employee" ASC NULLS FIRST'),
            ({"grouping": "cube"}, 'ORDER BY "Employee" ASC NULLS FIRST'),
        ],
        ids=["limit", "rollup", "cube"],
    )
    def test_default_order_by_coalesce_alias(self, extra: dict[str, object], order_by: str) -> None:
        # Limited or rolled up, a query is sorted by its dimensions: a
        # coalesced one by its alias, once. Its members are not in the outer
        # SELECT, and DuckDB, PostgreSQL and Dremio refused them there.
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Sales Employee", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Quantity"],
            ),
            **extra,
        )
        sql = CompilationPipeline().compile(query, model, "postgres").sql
        assert sql[sql.rindex("ORDER BY") :].splitlines()[0] == order_by

    @pytest.mark.parametrize(
        ("extra", "rows"),
        [
            ({"limit": 2}, [("a", "b", 15, 20), ("n", "s", 10, 20)]),
            (
                {"grouping": "rollup"},
                [
                    (None, None, 25, 20, 1, 1),
                    ("a", None, 15, 20, 0, 1),
                    ("a", "b", 15, 20, 0, 0),
                    ("n", None, 10, 20, 0, 1),
                    ("n", "s", 10, 20, 0, 0),
                ],
            ),
            (
                {"grouping": "cube"},
                [
                    (None, None, 25, 20, 1, 1),
                    (None, "b", 15, 20, 1, 0),
                    (None, "s", 10, 20, 1, 0),
                    ("a", None, 15, 20, 0, 1),
                    ("a", "b", 15, 20, 0, 0),
                    ("n", None, 10, 20, 0, 1),
                    ("n", "s", 10, 20, 0, 0),
                ],
            ),
        ],
        ids=["limit", "rollup", "cube"],
    )
    def test_default_order_by_members_where_cfl_falls_back_to_a_star(
        self, extra: dict[str, object], rows: list[tuple[object, ...]]
    ) -> None:
        # Sorted by the alias, which the star does not return, every row was
        # refused: the plan, not the query, says what the columns are.
        raw, src = TrackedLoader().load_string(STAR_FALLBACK_MODEL)
        model, result = ReferenceResolver().resolve(raw, src)
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(
                dimensions=[CoalesceDimension(coalesce=["X", "Y"], alias="XY")],
                measures=["Revenue", "Spend"],
            ),
            **extra,
        )
        assert QueryResolver().resolve(query, model).requires_cfl
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert "UNION" not in sql
        con = duckdb.connect()
        con.execute("CREATE TABLE a (x VARCHAR, y VARCHAR, amount INT)")
        con.execute("INSERT INTO a VALUES ('n', 's', 10), ('a', 'b', 15)")
        con.execute("CREATE TABLE b AS SELECT 20 AS amount")
        assert con.execute(sql).fetchall() == rows

    def test_each_leg_still_projects_only_its_own_role(self) -> None:
        # The leg-level fix (via-aware projection) must still hold even when
        # the outer wrapper coalesces.
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Sales Employee", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Quantity"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        # Leg 1 (Sales): Purchase Employee projects as NULL
        assert 'CAST(NULL AS VARCHAR) AS "Purchase Employee"' in result.sql
        # Leg 2 (Purchases): Sales Employee projects as NULL
        assert 'CAST(NULL AS VARCHAR) AS "Sales Employee"' in result.sql


# Sales and purchases by region, a fact each: n has both, s sales only, e
# purchases only. A total, a grain, a window and a running total over them.
STACKED_MODEL = """\
version: 1.0
dataObjects:
  Sales:
    code: s
    columns:
      Region: {code: region, abstractType: string}
      Day: {code: day, abstractType: date}
      Amount: {code: amount, abstractType: int}
  Purchases:
    code: p
    columns:
      Region: {code: region, abstractType: string}
      Day: {code: day, abstractType: date}
      Cost: {code: cost, abstractType: int}
dimensions:
  Sales Region: {dataObject: Sales, column: Region, resultType: string}
  Purchase Region: {dataObject: Purchases, column: Region, resultType: string}
  Sales Day: {dataObject: Sales, column: Day, resultType: date}
  Purchase Day: {dataObject: Purchases, column: Day, resultType: date}
measures:
  Revenue: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum}
  Spend: {columns: [{dataObject: Purchases, column: Cost}], aggregation: sum}
  Revenue Total: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum, total: true}
  Spend Median:
    columns: [{dataObject: Purchases, column: Cost}]
    aggregation: median
    total: true
  Revenue by Sales Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    grain: {mode: FIXED, keepOnly: [Sales Region]}
metrics:
  Revenue Rank: {type: window, windowFunction: dense_rank, measure: Revenue}
  Revenue Share: {expression: "{[Revenue]} / {[Revenue Total]}"}
  Running Revenue: {type: cumulative, measure: Revenue, timeDimension: Sales Day}
  Previous Revenue:
    {type: window, windowFunction: lag, measure: Revenue, timeDimension: Sales Day, offset: 1}
"""
STACKED_REGION = CoalesceDimension(coalesce=["Sales Region", "Purchase Region"], alias="Region")


@pytest.fixture(scope="module")
def stacked_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE s (region VARCHAR, day DATE, amount INT)")
    con.execute(
        "INSERT INTO s VALUES ('n', '2024-01-05', 10), ('n', '2024-02-03', 20), "
        "('s', '2024-02-10', 40)"
    )
    con.execute("CREATE TABLE p (region VARCHAR, day DATE, cost INT)")
    con.execute("INSERT INTO p VALUES ('n', '2024-01-07', 5), ('e', '2024-03-01', 7)")
    return con


def _stacked_sql(
    dimensions: list[str | CoalesceDimension], measures: list[str], **extra: object
) -> str:
    raw, src = TrackedLoader().load_string(STACKED_MODEL)
    model, result = ReferenceResolver().resolve(raw, src)
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures), **extra)
    return CompilationPipeline().compile(query, model, "duckdb").sql


class TestCoalescedBesideTheWrappers:
    """Over two facts the plan returns a coalesce group as one column, its
    alias. The totals and window passes projected, partitioned and joined by
    the group's members, which their query does not have, and every such query
    failed."""

    @pytest.mark.parametrize(
        ("measure", "by_region"),
        [
            ("Revenue Total", {"e": 70, "n": 70, "s": 70}),
            ("Spend Median", {"e": 6.0, "n": 6.0, "s": 6.0}),
            ("Revenue Rank", {"e": 3, "n": 2, "s": 1}),
            ("Revenue Share", {"e": None, "n": Decimal("0.428571"), "s": Decimal("0.571429")}),
            ("Revenue by Sales Region", {"e": None, "n": 30, "s": 40}),
        ],
        ids=["windowed_total", "total_apart", "window_metric", "metric_over_total", "grain"],
    )
    def test_each_region_reads_its_value(
        self, stacked_con: duckdb.DuckDBPyConnection, measure: str, by_region: dict[str, object]
    ) -> None:
        sql = _stacked_sql([STACKED_REGION], [measure, "Revenue", "Spend"])

        assert {r[0]: r[1] for r in stacked_con.execute(sql).fetchall()} == by_region

    @pytest.mark.parametrize(
        ("measure", "whole"),
        [("Revenue Total", 70), ("Spend Median", 6.0), ("Revenue Share", Decimal("1.000000"))],
        ids=["windowed_total", "total_apart", "metric_over_total"],
    )
    def test_under_rollup_the_grand_total_row_too(
        self, stacked_con: duckdb.DuckDBPyConnection, measure: str, whole: object
    ) -> None:
        sql = _stacked_sql([STACKED_REGION], [measure, "Revenue", "Spend"], grouping="rollup")

        rows = stacked_con.execute(sql).fetchall()

        # Region, the measure, Revenue, Spend, the Region's flag: the grand total is 1.
        assert [r[1] for r in rows if r[4] == 1] == [whole]
        assert len(rows) == 4

    @pytest.mark.parametrize(
        ("dimensions", "metric"),
        [
            ([STACKED_REGION, "Sales Day:month"], "Running Revenue"),
            (
                [CoalesceDimension(coalesce=["Sales Day", "Purchase Day"], alias="Day")],
                "Previous Revenue",
            ),
        ],
        ids=["running_total", "window_over_a_coalesced_date"],
    )
    def test_a_running_total_or_a_window_over_its_dates_is_refused(
        self, dimensions: list[str | CoalesceDimension], metric: str
    ) -> None:
        """Their passes read the group's members by name too, through several
        steps: refused, rather than compiled to SQL naming absent columns."""
        with pytest.raises(ResolutionError) as caught:
            _stacked_sql(dimensions, [metric, "Spend"])

        assert [e.code for e in caught.value.errors] == ["INCOMPATIBLE_COMBINATION"]
        assert f"'{metric}'" in caught.value.errors[0].message


class TestCoalesceValidation:
    def test_too_few_members(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[CoalesceDimension(coalesce=["Employee Name"], alias="Solo")],
                measures=["Total Sales"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert "COALESCE_TOO_FEW_MEMBERS" in {e.code for e in exc.value.errors}

    def test_alias_collision_with_existing_dimension(self) -> None:
        model = _load_model()
        # "Employee Name" is already a model dimension — using it as an alias
        # must be rejected.
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Employee Name", "Purchase Employee"],
                        alias="Employee Name",
                    )
                ],
                measures=["Total Sales"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert "COALESCE_ALIAS_COLLISION" in {e.code for e in exc.value.errors}

    def test_duplicate_alias_in_same_query(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Sales Employee", "Purchase Employee"], alias="Employee"
                    ),
                    CoalesceDimension(
                        coalesce=["Return Employee", "Shipment Employee"], alias="Employee"
                    ),
                ],
                measures=["Total Sales"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert "DUPLICATE_COALESCE_ALIAS" in {e.code for e in exc.value.errors}
