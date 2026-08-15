"""Choosing among several roles of one data object.

When two facts join the same conformed dimension, that dimension is reachable
by more than one route, and the routes are different *roles* — ``date_dim`` as
the sold date and as the returned date. Which one a plain reference means is a
question only the query can answer.

Before this, the answer came from iterating a set: string hashing is randomised
per process, so the same model and query compiled to a different role from run
to run. These tests pin the two halves of the fix — the choice is anchored on
the query's base object, and a genuine tie is refused rather than guessed.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

PIPELINE = CompilationPipeline()

# Sales and Returns each join Dates on their own date key. Returns hangs off
# Sales, so from the base the sold date is one hop and the returned date two.
TWO_FACTS = """\
version: 1.0

dataObjects:
  Dates:
    code: DATES
    database: WH
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: int, primaryKey: true}
      Year: {code: YEAR, abstractType: int}

  Items:
    code: ITEMS
    database: WH
    schema: PUBLIC
    columns:
      Item Key: {code: ITEM_KEY, abstractType: int, primaryKey: true}
      Item Name: {code: ITEM_NAME, abstractType: string}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Sale Key: {code: SALE_KEY, abstractType: int}
      Sold Date Key: {code: SOLD_DATE_KEY, abstractType: int}
      Sale Item Key: {code: ITEM_KEY, abstractType: int}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Sold Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Items
        columnsFrom: [Sale Item Key]
        columnsTo: [Item Key]
      - joinType: many-to-one
        joinTo: Returns
        columnsFrom: [Sale Key]
        columnsTo: [Returned Sale Key]

  Returns:
    code: RETURNS
    database: WH
    schema: PUBLIC
    columns:
      Returned Sale Key: {code: SALE_KEY, abstractType: int}
      Returned Date Key: {code: RETURNED_DATE_KEY, abstractType: int}
      Refund: {code: REFUND, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Returned Date Key]
        columnsTo: [Date Key]

dimensions:
  Year: {dataObject: Dates, column: Year, resultType: int}
  Item Name: {dataObject: Items, column: Item Name, resultType: string}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Refund Amount:
    columns: [{dataObject: Returns, column: Refund}]
    resultType: float
    aggregation: sum
"""

# One fact, two dimensions at the same distance from it, both joining Region.
# The store's region and the supplier's region are different roles, and the
# base has no reason to prefer either — the tie a plain reference cannot break.
SYMMETRIC = """\
version: 1.0

dataObjects:
  Region:
    code: REGION
    database: WH
    schema: PUBLIC
    columns:
      Region Key: {code: REGION_KEY, abstractType: int, primaryKey: true}
      Region Name: {code: REGION_NAME, abstractType: string}

  Store:
    code: STORE
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: STORE_KEY, abstractType: int, primaryKey: true}
      Store Region Key: {code: REGION_KEY, abstractType: int}
      Store Name: {code: STORE_NAME, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Region
        columnsFrom: [Store Region Key]
        columnsTo: [Region Key]

  Supplier:
    code: SUPPLIER
    database: WH
    schema: PUBLIC
    columns:
      Supplier Key: {code: SUPPLIER_KEY, abstractType: int, primaryKey: true}
      Supplier Region Key: {code: REGION_KEY, abstractType: int}
      Supplier Name: {code: SUPPLIER_NAME, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Region
        columnsFrom: [Supplier Region Key]
        columnsTo: [Region Key]

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Sold Store Key: {code: STORE_KEY, abstractType: int}
      Sold Supplier Key: {code: SUPPLIER_KEY, abstractType: int}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Sold Store Key]
        columnsTo: [Store Key]
      - joinType: many-to-one
        joinTo: Supplier
        columnsFrom: [Sold Supplier Key]
        columnsTo: [Supplier Key]

dimensions:
  Store Name: {dataObject: Store, column: Store Name, resultType: string}
  Supplier Name: {dataObject: Supplier, column: Supplier Name, resultType: string}
  Region Name: {dataObject: Region, column: Region Name, resultType: string}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
"""


def _load(yaml_str: str) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_str)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


class TestDeterministicRole:
    """The nearer role wins, and it wins every time."""

    def _sql(self) -> str:
        return PIPELINE.compile(
            QueryObject(
                select=QuerySelect(
                    dimensions=["Item Name"], measures=["Sales Amount", "Refund Amount"]
                ),
                where=[QueryFilter(field="Year", op=FilterOperator.EQUALS, value=2024)],
            ),
            _load(TWO_FACTS),
            "postgres",
        ).sql

    def test_binds_to_the_role_the_base_reaches_directly(self) -> None:
        sql = self._sql()
        assert '"Sales"."SOLD_DATE_KEY" = "Dates"."DATE_KEY"' in sql
        assert "RETURNED_DATE_KEY" not in sql

    def test_same_answer_every_time(self) -> None:
        """The choice used to come from set iteration order, so it moved
        between processes. Repeating it in one process cannot prove that is
        gone, but a stable graph-level ranking can."""
        assert len({self._sql() for _ in range(5)}) == 1

    def test_ranking_prefers_the_anchor(self) -> None:
        graph = JoinGraph(_load(TWO_FACTS))
        roles = graph.role_candidates({"Sales", "Returns"}, "Dates", prefer_from="Sales")
        assert [path[0] for path in roles] == ["Sales"]

    def test_ranking_without_an_anchor_reports_both(self) -> None:
        """No anchor, no reason to prefer either — the caller has to decide."""
        graph = JoinGraph(_load(TWO_FACTS))
        roles = graph.role_candidates({"Sales", "Returns"}, "Dates")
        assert sorted(path[0] for path in roles) == ["Returns", "Sales"]


class TestAmbiguousRoleRefused:
    """Equidistant roles are refused, not guessed."""

    def test_filter_on_an_ambiguous_object_is_refused(self) -> None:
        """The query joins both Store and Supplier, and Region sits one hop
        past each. The store's region and the supplier's region select
        different rows, so the filter is refused rather than guessed."""
        with pytest.raises(ResolutionError) as excinfo:
            PIPELINE.compile(
                QueryObject(
                    select=QuerySelect(
                        dimensions=["Store Name", "Supplier Name"], measures=["Sales Amount"]
                    ),
                    where=[
                        QueryFilter(field="Region Name", op=FilterOperator.EQUALS, value="North")
                    ],
                ),
                _load(SYMMETRIC),
                "postgres",
            )
        ambiguous = [e for e in excinfo.value.errors if e.code == "AMBIGUOUS_JOIN_PATH"]
        assert ambiguous, [e.code for e in excinfo.value.errors]
        message = ambiguous[0].message
        assert "Store" in message and "Supplier" in message
        assert ambiguous[0].hint is not None and "via" in ambiguous[0].hint

    def test_one_route_is_not_ambiguous(self) -> None:
        """A dimension only one joined object reaches compiles as before."""
        sql = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Item Name"], measures=["Sales Amount"]),
                where=[QueryFilter(field="Year", op=FilterOperator.EQUALS, value=2024)],
            ),
            _load(TWO_FACTS),
            "postgres",
        ).sql
        assert '"Dates"."YEAR" = 2024' in sql
