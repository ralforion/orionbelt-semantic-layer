"""ORDER BY a measure or metric the query does not select.

"The two customers with the most revenue", returning only their names: the
measure is computed in a query of its own over the same dimensions, joined onto
the query's rows and sorted on (``compiler/order_only.py``). The rows stay the
query's own: Dan, who ordered nothing, keeps his row and sorts as NULL. A
dimension the query does not select is still refused, as is ROLLUP / CUBE.

=========  ========  ======  =========  ======
order      customer  amount  date       return
=========  ========  ======  =========  ======
``o1``     Ann       10      Jan 5      -
``o2``     Bob       50      Jan 9      2
``o3``     Bob       5       Feb 1      -
``o4``     Cy        30      Feb 3      1
*(none)*   Dan
=========  ========  ======  =========  ======
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: order_by_unselected
dataObjects:
  Customers:
    code: customers
    columns:
      Cust Id: {code: id, abstractType: string, primaryKey: true}
      Name: {code: name, abstractType: string}
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer_id, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust Id]}
  Returns:
    code: returns
    columns:
      Return Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer_id, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust Id]}
dimensions:
  Customer Name: {dataObject: Customers, column: Name, resultType: string}
  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum, resultType: float}
  Returned: {columns: [{dataObject: Returns, column: Quantity}], aggregation: sum, resultType: int}
metrics:
  Revenue per Order: {type: derived, expression: '{[Revenue]} / {[Orders Count]}'}
  Running Revenue: {type: cumulative, measure: Revenue, timeDimension: Order Month}
  Revenue Rank:
    type: window
    measure: Revenue
    windowFunction: rank
    orderDirection: desc
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customers AS SELECT * FROM"
        " (VALUES ('c1', 'Ann'), ('c2', 'Bob'), ('c3', 'Cy'), ('c4', 'Dan')) t(id, name)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES"
        " ('o1', 'c1', DATE '2025-01-05', 10.0), ('o2', 'c2', DATE '2025-01-09', 50.0),"
        " ('o3', 'c2', DATE '2025-02-01', 5.0), ('o4', 'c3', DATE '2025-02-03', 30.0))"
        " t(id, customer_id, order_date, amount)"
    )
    con.execute(
        "CREATE TABLE returns AS SELECT * FROM"
        " (VALUES ('r1', 'c2', 2), ('r2', 'c3', 1)) t(id, customer_id, quantity)"
    )
    return con


def _compile(model: SemanticModel, query: dict) -> CompilationResult:
    return CompilationPipeline().compile(QueryObject(**query), model, "duckdb")


def _rows(model: SemanticModel, con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple]:
    return con.execute(_compile(model, query).sql).fetchall()


def _by(field: str, direction: str = "asc") -> dict:
    return {"field": field, "direction": direction}


def test_top_n_by_an_unselected_measure(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "orderBy": [_by("Revenue", "desc")],
        "limit": 2,
    }
    result = con.execute(_compile(model, query).sql)

    # Only the selected column comes back.
    assert [column[0] for column in result.description] == ["Customer Name"]
    assert result.fetchall() == [("Bob",), ("Cy",)]


@pytest.mark.parametrize(
    ("dimension", "metric", "expected"),
    [
        ("Customer Name", "Revenue per Order", [("Cy",), ("Bob",), ("Ann",), ("Dan",)]),
        ("Order Month", "Running Revenue", [(date(2025, 2, 1),), (date(2025, 1, 1),)]),
        ("Customer Name", "Revenue Rank", [("Ann",), ("Cy",), ("Bob",), ("Dan",)]),
    ],
    ids=["derived", "cumulative", "window"],
)
def test_an_unselected_metric(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    dimension: str,
    metric: str,
    expected: list[tuple],
) -> None:
    query = {"select": {"dimensions": [dimension]}, "orderBy": [_by(metric, "desc")]}
    assert _rows(model, con, query) == expected


def test_beside_selected_measures_and_a_position(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"], "measures": ["Orders Count"]},
        "orderBy": [_by("Orders Count", "desc"), _by("Revenue", "desc"), _by("1")],
    }
    # Bob has two orders; Cy's 30 beats Ann's 10 between the one-order customers.
    assert _rows(model, con, query) == [("Bob", 2), ("Cy", 1), ("Ann", 1)]


def test_a_position_counts_the_returned_columns(model: SemanticModel) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "orderBy": [_by("Revenue"), _by("2")],
    }
    with pytest.raises(ResolutionError) as excinfo:
        _compile(model, query)
    assert [e.code for e in excinfo.value.errors] == ["INVALID_ORDER_BY_POSITION"]


def test_a_measure_of_another_fact(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"], "measures": ["Revenue"]},
        "orderBy": [_by("Returned", "desc"), _by("Customer Name")],
    }
    compiled = _compile(model, query)

    # The rows are the revenue query's; the returns only sort them.
    assert compiled.explain is not None
    assert compiled.explain.planner == "Star Schema"
    assert "returns" in compiled.physical_tables
    # Bob returned 2, Cy 1, Ann nothing (NULL, last when descending on DuckDB).
    assert con.execute(compiled.sql).fetchall() == [("Bob", 55.0), ("Cy", 30.0), ("Ann", 10.0)]


def test_a_row_without_the_sort_measure_keeps_its_place(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "orderBy": [
            {"field": "Returned", "direction": "desc", "nulls": "last"},
            _by("Customer Name"),
        ],
    }
    # Every customer, as without the sort: Ann and Dan returned nothing.
    assert _rows(model, con, query) == [("Bob",), ("Cy",), ("Ann",), ("Dan",)]


def test_with_having_on_another_unselected_measure(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "having": [{"field": "Orders Count", "op": "=", "value": 1}],
        "orderBy": [_by("Revenue", "desc")],
    }
    assert _rows(model, con, query) == [("Cy",), ("Ann",)]


def test_under_rollup_is_refused(model: SemanticModel) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "grouping": "rollup",
        "orderBy": [_by("Revenue", "desc")],
    }
    with pytest.raises(ResolutionError) as excinfo:
        _compile(model, query)
    assert [e.code for e in excinfo.value.errors] == ["ORDER_BY_COMBINATION_NOT_SUPPORTED"]


def test_with_fill(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> None:
    query = {
        "select": {"dimensions": ["Order Month"], "measures": ["Orders Count"]},
        "where": [{"field": "Order Month", "op": "between", "value": ["2024-12-01", "2025-02-01"]}],
        "fill": True,
        "orderBy": [_by("Revenue", "desc"), _by("Order Month")],
    }
    # December has no orders: its revenue is NULL and sorts last.
    assert _rows(model, con, query) == [
        (date(2025, 1, 1), 2),
        (date(2025, 2, 1), 2),
        (date(2024, 12, 1), 0),
    ]


def test_an_unselected_dimension_is_refused(model: SemanticModel) -> None:
    query = {"select": {"measures": ["Revenue"]}, "orderBy": [_by("Customer Name")]}
    with pytest.raises(ResolutionError) as excinfo:
        _compile(model, query)
    assert [e.code for e in excinfo.value.errors] == ["UNKNOWN_ORDER_BY_FIELD"]


def test_a_selected_measure_keeps_the_plain_plan(model: SemanticModel) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"], "measures": ["Revenue"]},
        "orderBy": [_by("Revenue", "desc")],
    }
    assert "sorted_rows" not in _compile(model, query).sql


def test_two_dimensions_join_on_both(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> None:
    query = {
        "select": {"dimensions": ["Customer Name", "Order Month"]},
        "orderBy": [_by("Revenue", "desc")],
    }
    sql = _compile(model, query).sql
    # Each dimension's null-safe match stays in its own parentheses beside the AND.
    assert ") AND (" in sql
    assert con.execute(sql).fetchall() == [
        ("Bob", date(2025, 1, 1)),
        ("Cy", date(2025, 2, 1)),
        ("Ann", date(2025, 1, 1)),
        ("Bob", date(2025, 2, 1)),
    ]


def test_a_having_only_measure_sorts_beside_a_hidden_one(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "having": [{"field": "Orders Count", "op": ">=", "value": 1}],
        "orderBy": [_by("Orders Count", "desc"), _by("Revenue", "desc")],
    }
    result = con.execute(_compile(model, query).sql)
    assert [column[0] for column in result.description] == ["Customer Name"]
    assert result.fetchall() == [("Bob",), ("Cy",), ("Ann",)]


def test_a_hidden_cumulative_metric_reads_as_of(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "asOf": "2025-01-31",
        "orderBy": [{"field": "Running Revenue", "direction": "desc", "nulls": "last"}],
    }
    compiled = _compile(model, query)
    # As of January: Bob 50, Ann 10, Cy and Dan nothing yet. February's totals
    # would put Cy (30) ahead of Ann.
    assert con.execute(compiled.sql).fetchall() == [("Bob",), ("Ann",), ("Cy",), ("Dan",)]
    assert not any("asOf" in w.message for w in compiled.warnings)


def test_a_filled_period_carries_its_cumulative_sort_value(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    query = {
        "select": {"dimensions": ["Order Month"]},
        "where": [{"field": "Order Month", "op": "between", "value": ["2025-01-01", "2025-03-01"]}],
        "fill": True,
        "orderBy": [_by("Running Revenue", "desc"), _by("Order Month", "desc")],
    }
    # March has no orders and carries February's 95, ahead of January's 60.
    assert _rows(model, con, query) == [
        (date(2025, 3, 1),),
        (date(2025, 2, 1),),
        (date(2025, 1, 1),),
    ]


def test_every_table_of_the_sort_query_is_a_cache_dependency() -> None:
    bridged = """
version: "1.0"
dataObjects:
  Customers:
    code: customers
    columns:
      Cust Id: {code: id, abstractType: string, primaryKey: true}
      Name: {code: name, abstractType: string}
  Customer Links:
    code: customer_links
    columns:
      Link Id: {code: id, abstractType: string, primaryKey: true}
      Linked Customer: {code: customer_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Linked Customer]
        columnsTo: [Cust Id]
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Link: {code: link_id, abstractType: string}
      Amount: {code: amount, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Customer Links, columnsFrom: [Link], columnsTo: [Link Id]}
dimensions:
  Customer Name: {dataObject: Customers, column: Name, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum, resultType: float}
"""
    raw, source_map = TrackedLoader().load_string(bridged)
    bridged_model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = {"select": {"dimensions": ["Customer Name"]}, "orderBy": [_by("Revenue", "desc")]}
    compiled = _compile(bridged_model, query)

    # The sort query reaches the customers through the links: a change there
    # reorders the rows.
    assert '"customer_links"' in compiled.sql
    assert {"customers", "customer_links", "orders"} <= set(compiled.physical_tables)
    # The same holds for the bridge of a query selecting the measure.
    selected = _compile(
        bridged_model, {"select": {"dimensions": ["Customer Name"], "measures": ["Revenue"]}}
    )
    assert "customer_links" in selected.physical_tables


BRIDGED_RETURNS = """
version: "1.0"
dataObjects:
  Customers:
    code: customers
    columns:
      Cust Id: {code: id, abstractType: string, primaryKey: true}
      Name: {code: name, abstractType: string}
  Customer Links:
    code: customer_links
    columns:
      Link Id: {code: id, abstractType: string, primaryKey: true}
      Linked Customer: {code: customer_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Linked Customer]
        columnsTo: [Cust Id]
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer_id, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust Id]}
  Returns:
    code: returns
    columns:
      Return Id: {code: id, abstractType: string, primaryKey: true}
      Link: {code: link_id, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Customer Links, columnsFrom: [Link], columnsTo: [Link Id]}
dimensions:
  Customer Name: {dataObject: Customers, column: Name, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum, resultType: float}
  Returned: {columns: [{dataObject: Returns, column: Quantity}], aggregation: sum, resultType: int}
"""

JANUARY = [{"field": "Orders.Order Date", "op": "<", "value": "2025-02-01"}]


@pytest.fixture(scope="module")
def bridged() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(BRIDGED_RETURNS)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.mark.parametrize(
    "query",
    [
        {"select": {"dimensions": ["Customer Name"], "measures": ["Revenue", "Returned"]}},
        {"select": {"dimensions": ["Customer Name"]}, "orderBy": [_by("Revenue"), _by("Returned")]},
    ],
    ids=["selected", "sorted"],
)
def test_a_bridge_in_another_cfl_leg_is_a_cache_dependency(
    bridged: SemanticModel, query: dict
) -> None:
    # The Returns leg reaches the customers through the links.
    assert "customer_links" in _compile(bridged, query).physical_tables


def test_a_filter_the_sort_fact_cannot_apply_is_reported(bridged: SemanticModel) -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customers AS SELECT * FROM"
        " (VALUES ('c1', 'Ann'), ('c2', 'Bob'), ('c3', 'Cy')) t(id, name)"
    )
    con.execute(
        "CREATE TABLE customer_links AS SELECT * FROM"
        " (VALUES ('l1', 'c1'), ('l2', 'c2'), ('l3', 'c3')) t(id, customer_id)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES ('o1', 'c1', DATE '2025-01-05', 10.0),"
        " ('o2', 'c2', DATE '2025-01-09', 50.0), ('o3', 'c3', DATE '2025-02-03', 30.0))"
        " t(id, customer_id, order_date, amount)"
    )
    con.execute(
        "CREATE TABLE returns AS SELECT * FROM"
        " (VALUES ('r1', 'l3', 5), ('r2', 'l2', 1)) t(id, link_id, quantity)"
    )
    sorted_query = {
        "select": {"dimensions": ["Customer Name"], "measures": ["Revenue"]},
        "where": JANUARY,
        "orderBy": [{"field": "Returned", "direction": "desc", "nulls": "last"}],
    }
    compiled = _compile(bridged, sorted_query)
    selected = _compile(
        bridged,
        {
            "select": {"dimensions": ["Customer Name"], "measures": ["Revenue", "Returned"]},
            "where": JANUARY,
        },
    )

    # The returns cannot be restricted to January orders, as when selected.
    codes = [w.code for w in compiled.warnings]
    assert "FILTER_NOT_APPLIED" in codes
    assert codes.count("FILTER_NOT_APPLIED") == [w.code for w in selected.warnings].count(
        "FILTER_NOT_APPLIED"
    )
    # January's customers, ranked by all their returns: Bob 1, Ann none.
    assert con.execute(compiled.sql).fetchall() == [("Bob", 50.0), ("Ann", 10.0)]


def test_a_having_only_measure_is_planning_context_of_the_sort(
    bridged: SemanticModel,
) -> None:
    # Revenue, read only by HAVING, keeps the Orders fact in the plan the sort
    # values come from, as it does when Returned is selected.
    query = {
        "select": {"dimensions": ["Customer Name"]},
        "where": JANUARY,
        "having": [{"field": "Revenue", "op": ">", "value": 0}],
        "orderBy": [_by("Returned", "desc")],
    }
    compiled = _compile(bridged, query)
    assert "FILTER_NOT_APPLIED" in [w.code for w in compiled.warnings]
