"""A dimension read ``via`` a fact, in a query over several facts.

Each fact is a leg of the union, and a leg that cannot reach the dimension's
``via`` object pads it. When no leg reaches it, every leg pads it: under
``UNION ALL BY NAME`` (DuckDB) the union then had no such column and the outer
SELECT named its own alias, which DuckDB refused ("cannot be referenced before
it is defined"); on the other dialects every row was grouped under NULL. Both
are refused now, as the same query over one fact always was.

Sales, Purchases and Returns each join Channels, and none of them another.
``Purchase Channel`` is the channel read via Purchases, ``Channel`` the plain
one.

=======  =====  ==========  =======
channel  sales  purchases   returns
=======  =====  ==========  =======
Web      10     3           1
Shop     20     5, 7        2
=======  =====  ==========  =======
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
name: cfl_via_dimension
dataObjects:
  Channels:
    code: channels
    columns:
      Channel Id: {code: id, abstractType: string, primaryKey: true}
      Channel Name: {code: name, abstractType: string}
  Sales:
    code: sales
    columns:
      Sale Id: {code: id, abstractType: string, primaryKey: true}
      Sale Channel Id: {code: channel_id, abstractType: string}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Channels
        columnsFrom: [Sale Channel Id]
        columnsTo: [Channel Id]
  Purchases:
    code: purchases
    columns:
      Purchase Id: {code: id, abstractType: string, primaryKey: true}
      Purchase Channel Id: {code: channel_id, abstractType: string}
      Cost: {code: cost, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Channels
        columnsFrom: [Purchase Channel Id]
        columnsTo: [Channel Id]
  Returns:
    code: returns
    columns:
      Return Id: {code: id, abstractType: string, primaryKey: true}
      Return Channel Id: {code: channel_id, abstractType: string}
      Refund: {code: refund, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Channels
        columnsFrom: [Return Channel Id]
        columnsTo: [Channel Id]
dimensions:
  Channel: {dataObject: Channels, column: Channel Name, resultType: string}
  Purchase Channel: {dataObject: Channels, column: Channel Name, resultType: string, via: Purchases}
measures:
  Revenue:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    resultType: float
  Spend:
    columns: [{dataObject: Purchases, column: Cost}]
    aggregation: sum
    resultType: float
  Refunds:
    columns: [{dataObject: Returns, column: Refund}]
    aggregation: sum
    resultType: float
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE channels AS SELECT * FROM (VALUES ('c1', 'Web'), ('c2', 'Shop')) t(id, name)"
    )
    con.execute(
        "CREATE TABLE sales AS SELECT * FROM"
        " (VALUES ('s1', 'c1', 10.0), ('s2', 'c2', 20.0)) t(id, channel_id, amount)"
    )
    con.execute(
        "CREATE TABLE purchases AS SELECT * FROM"
        " (VALUES ('p1', 'c1', 3.0), ('p2', 'c2', 5.0), ('p3', 'c2', 7.0)) t(id, channel_id, cost)"
    )
    con.execute(
        "CREATE TABLE returns AS SELECT * FROM"
        " (VALUES ('r1', 'c1', 1.0), ('r2', 'c2', 2.0)) t(id, channel_id, refund)"
    )
    return con


def _compile(dimensions: list[str], measures: list[str], dialect: str) -> CompilationResult:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject(**{"select": {"dimensions": dimensions, "measures": measures}})
    return CompilationPipeline().compile(query, model, dialect)


@pytest.mark.parametrize("dialect", DialectRegistry.available())
def test_no_fact_of_the_query_reaches_the_via_object(dialect: str) -> None:
    with pytest.raises(ResolutionError) as excinfo:
        _compile(["Purchase Channel"], ["Revenue", "Refunds"], dialect)
    [error] = excinfo.value.errors
    assert error.code == "UNREACHABLE_REQUIRED_OBJECT"
    assert "'Purchase Channel'" in error.message
    assert "'Purchases'" in error.message


def test_one_fact_that_reaches_it_groups_by_it(con: duckdb.DuckDBPyConnection) -> None:
    """The Purchases leg projects the channel and the Returns leg pads it, so
    Spend is split by channel and the refunds land in the NULL row."""
    compiled = _compile(["Purchase Channel"], ["Spend", "Refunds"], "duckdb")
    rows = con.execute(compiled.sql).fetchall()
    assert sorted(rows, key=lambda row: row[0] or "") == [
        (None, None, 3.0),
        ("Shop", 12.0, None),
        ("Web", 3.0, None),
    ]


def test_the_plain_dimension_is_grouped_by_every_leg(con: duckdb.DuckDBPyConnection) -> None:
    """``Channel`` is the one to group Revenue and Refunds by."""
    compiled = _compile(["Channel"], ["Revenue", "Refunds"], "duckdb")
    rows = con.execute(compiled.sql).fetchall()
    assert sorted(rows) == [("Shop", 20.0, 2.0), ("Web", 10.0, 1.0)]
