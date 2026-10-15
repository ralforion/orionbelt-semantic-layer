"""A WHERE filter the caller wrote is applied or refused, never dropped.

A query filter on an object the plan does not reach used to be skipped as
"irrelevant to the current query". With measures on two objects the skip fired
on filters that were anything but: counting customers and orders
``WHERE status = 'ok'`` based the query on Customers, could not reach Orders
from there, and dropped the status filter from every leg of the union, so
every count and sum came back unfiltered. Across two independent facts the
opposite happened: the predicate was copied into a leg that never joins its
table.

Static model filters keep their contract: they apply where relevant.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Regions:
    code: regions
    schema: main
    columns:
      Region ID: {code: id, abstractType: int, primaryKey: true}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Customer Region ID: {code: region_id, abstractType: int}
      Tier: {code: tier, abstractType: string}
    # One join each for Customers and Orders: the "most joins" tie-break then
    # picks Customers as the base, which cannot reach the Orders filter.
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Amount: {code: amount, abstractType: float}
      Status: {code: status, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: int, primaryKey: true}
      Return Customer ID: {code: customer_id, abstractType: int}
      Refund: {code: refund, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Tier: {dataObject: Customers, column: Tier, resultType: string}
  Status: {dataObject: Orders, column: Status, resultType: string}
measures:
  Customer Count:
    columns: [{dataObject: Customers, column: Customer ID}]
    aggregation: count
    resultType: int
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    aggregation: count
    resultType: int
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Refunds:
    columns: [{dataObject: Returns, column: Refund}]
    aggregation: sum
    resultType: float
"""


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw = YAML(typ="safe").load(yaml_text)
    model, result = ReferenceResolver().resolve(raw)
    assert not result.errors, result.errors
    return model


def _compile(query: dict, yaml_text: str = MODEL_YAML) -> CompilationResult:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), "duckdb")


def _sql(query: dict, yaml_text: str = MODEL_YAML) -> str:
    return _compile(query, yaml_text).sql


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions (id INTEGER)")
    con.execute("INSERT INTO regions VALUES (1)")
    con.execute("CREATE TABLE customers (id INTEGER, region_id INTEGER, tier VARCHAR)")
    # Carol has no orders at all.
    con.execute("INSERT INTO customers VALUES (1, 1, 'gold'), (2, 1, 'silver'), (3, 1, 'gold')")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE, status VARCHAR)"
    )
    # Bob has only a 'bad' order.
    con.execute(
        "INSERT INTO orders VALUES (1, 1, 100, 'ok'), (2, 1, 50, 'ok'), (3, 1, 7, 'bad'),"
        " (4, 2, 30, 'bad')"
    )
    con.execute("CREATE TABLE returns (id INTEGER, customer_id INTEGER, refund DOUBLE)")
    con.execute("INSERT INTO returns VALUES (1, 1, 10), (2, 2, 5)")
    return con


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    return [tuple(float(v) for v in row) for row in con.execute(sql).fetchall()]


def _rows_keyed(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    return [(row[0], *(float(v) for v in row[1:])) for row in con.execute(sql).fetchall()]


def test_many_side_filter_restricts_measures_on_both_objects(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """The filter's rows decide which customers count: those with an ok order."""
    sql = _sql(
        {
            "select": {"measures": ["Customer Count", "Order Count", "Revenue"]},
            "where": [{"field": "Status", "op": "=", "value": "ok"}],
        }
    )
    assert _rows(con, sql) == [(1.0, 2.0, 150.0)]


def test_one_side_filter_still_applies_to_every_leg(con: duckdb.DuckDBPyConnection) -> None:
    """Two independent facts both reach Customers, so both legs are restricted."""
    sql = _sql(
        {
            "select": {"measures": ["Revenue", "Refunds"]},
            "where": [{"field": "Tier", "op": "=", "value": "gold"}],
        }
    )
    assert _rows(con, sql) == [(157.0, 10.0)]


def test_filter_one_fact_cannot_reach_is_refused() -> None:
    """Returns never joins Orders. Refunds computed without the filter would
    answer a different question, and emitting the predicate there names a
    table the leg never joins."""
    with pytest.raises(ResolutionError) as excinfo:
        _sql(
            {
                "select": {"measures": ["Revenue", "Refunds"]},
                "where": [{"field": "Status", "op": "=", "value": "ok"}],
            }
        )
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["FILTER_NOT_APPLICABLE_TO_MEASURE"]
    assert "'Refunds'" in errors[0].message


def test_static_filter_one_fact_cannot_reach_applies_where_relevant(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A static model filter (``enforce: relevant``) is declared for every
    query, so the leg that cannot reach it skips it, and says so."""
    yaml_text = MODEL_YAML + (
        "filters:\n  - {dataObject: Orders, column: Status, operator: equals, value: ok}\n"
    )
    result = _compile({"select": {"measures": ["Revenue", "Refunds"]}}, yaml_text)
    assert _rows(con, result.sql) == [(150.0, 15.0)]
    skipped = [w for w in result.warnings if w.code == "STATIC_FILTER_NOT_APPLIED"]
    assert len(skipped) == 1
    assert "'Refunds'" in skipped[0].message


def test_enforced_static_filter_one_fact_cannot_reach_refuses_the_query() -> None:
    yaml_text = MODEL_YAML + (
        "filters:\n  - {dataObject: Orders, column: Status, operator: equals, value: ok,"
        " enforce: always}\n"
    )
    with pytest.raises(ResolutionError) as excinfo:
        _sql({"select": {"measures": ["Revenue", "Refunds"]}}, yaml_text)
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["STATIC_FILTER_UNREACHABLE"]
    assert "'Refunds'" in errors[0].message


@pytest.mark.parametrize("context", ["{mode: FIXED}", "{exclude: [Status]}"])
def test_enforced_static_filter_survives_a_filter_context(
    con: duckdb.DuckDBPyConnection, context: str
) -> None:
    """A FIXED scan of every row is what an enforced filter exists to prevent;
    a relevant one is dropped by the context as before."""
    measure = (
        "  All Revenue:\n"
        "    columns: [{dataObject: Orders, column: Amount}]\n"
        "    aggregation: sum\n"
        "    resultType: float\n"
        f"    filterContext: {context}\n"
        "  Refunds:\n"
    )
    status = "  - {dataObject: Orders, column: Status, operator: equals, value: ok"
    enforced = (
        MODEL_YAML.replace("  Refunds:\n", measure) + f"filters:\n{status}, enforce: always}}\n"
    )
    relevant = MODEL_YAML.replace("  Refunds:\n", measure) + f"filters:\n{status}}}\n"
    query = {"select": {"dimensions": ["Tier"], "measures": ["All Revenue"]}}
    assert sorted(_rows_keyed(con, _sql(query, enforced))) == [("gold", 150.0)]
    # The main query's filter still decides which rows exist (silver has no ok
    # order); the context only drops it inside the scan, so gold reads 157.
    assert sorted(_rows_keyed(con, _sql(query, relevant))) == [("gold", 157.0)]


def test_filter_on_a_disconnected_object_is_refused() -> None:
    """Nothing in the query reaches it, so nothing can honour it."""
    yaml_text = MODEL_YAML.replace(
        "dimensions:\n",
        "  Suppliers:\n"
        "    code: suppliers\n"
        "    schema: main\n"
        "    columns:\n"
        "      Supplier ID: {code: id, abstractType: int, primaryKey: true}\n"
        "      Supplier Name: {code: name, abstractType: string}\n"
        "dimensions:\n"
        "  Supplier Name: {dataObject: Suppliers, column: Supplier Name, resultType: string}\n",
    )
    with pytest.raises(ResolutionError) as excinfo:
        _sql(
            {
                "select": {"measures": ["Revenue"]},
                "where": [{"field": "Supplier Name", "op": "=", "value": "Acme"}],
            },
            yaml_text,
        )
    assert [e.code for e in excinfo.value.errors] == ["UNREACHABLE_FILTER_OBJECT"]


def test_query_filter_duplicating_a_static_one_keeps_its_demand() -> None:
    """Written in the query, the predicate is the caller's, even when the
    model declares the same one statically. Deduplicated to the static copy,
    it was skipped on the Refunds leg, which then came back unfiltered."""
    yaml_text = MODEL_YAML + (
        "filters:\n  - {dataObject: Orders, column: Status, operator: equals, value: ok}\n"
    )
    with pytest.raises(ResolutionError) as excinfo:
        _sql(
            {
                "select": {"measures": ["Revenue", "Refunds"]},
                "where": [{"field": "Status", "op": "=", "value": "ok"}],
            },
            yaml_text,
        )
    assert [e.code for e in excinfo.value.errors] == ["FILTER_NOT_APPLICABLE_TO_MEASURE"]


@pytest.mark.parametrize(
    "context",
    [
        "{mode: FIXED}",
        "{exclude: [Status]}",
        "{keepOnly: [Tier]}",
    ],
)
def test_filter_context_that_drops_the_filter_never_resolves_it(
    con: duckdb.DuckDBPyConnection, context: str
) -> None:
    """A filter-contexted measure's scan drops the Status filter, so that the
    scan cannot reach Orders is no reason to refuse it."""
    yaml_text = MODEL_YAML.replace(
        "  Refunds:\n",
        "  Unfiltered Refunds:\n"
        "    columns: [{dataObject: Returns, column: Refund}]\n"
        "    aggregation: sum\n"
        "    resultType: float\n"
        f"    filterContext: {context}\n"
        "  Refunds:\n",
    )
    sql = _sql(
        {
            "select": {"measures": ["Revenue", "Unfiltered Refunds"]},
            "where": [{"field": "Status", "op": "=", "value": "ok"}],
        },
        yaml_text,
    )
    assert _rows(con, sql) == [(150.0, 15.0)]
