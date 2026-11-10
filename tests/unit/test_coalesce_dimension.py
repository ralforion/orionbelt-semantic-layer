"""Tests for query-level coalesce dimensions.

Covers resolution validation, CFL outer-wrapper SQL emission, and
type-mismatch / alias-collision error paths.
"""

from __future__ import annotations

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
