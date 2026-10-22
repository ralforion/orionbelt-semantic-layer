"""A parent measure keeps the parents without children, whatever else is selected.

A query reading an order measure is based on the orders, and reaches the
customers through them: a customer without orders was in no row, so
``Customers Count`` was 3 alone and 2 beside ``Revenue``. Now those customers
are added as rows of their own (never joined to the orders, so nothing is
multiplied), and an order without a customer stays a row too, so every measure
adds up to what it reads alone. Grouped by an order column, the customers
without orders are its NULL group; a WHERE that no such customer passes leaves
them out.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Tier: {code: tier, abstractType: string}
      Credit: {code: credit, abstractType: float}
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: int, primaryKey: true}
      Category: {code: category, abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Order Product ID: {code: product_id, abstractType: int}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Order Product ID]
        columnsTo: [Product ID]
dimensions:
  Tier: {dataObject: Customers, column: Tier, resultType: string}
  Category: {dataObject: Products, column: Category, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Credit Plus One:
    expression: 'COALESCE({[Customers].[Credit]}, 0) + 1'
    aggregation: sum
    resultType: float
  All Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
    total: true
metrics:
  Credit And Orders:
    expression: '{[Total Credit]} + {[Orders Count]}'
  Order Score:
    expression: '{[Orders Count]} + 1'
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, tier VARCHAR, credit DOUBLE)")
    # Carol has no orders.
    con.execute("INSERT INTO customers VALUES (1, 'gold', 10), (2, 'silver', 20), (3, 'gold', 40)")
    con.execute("CREATE TABLE products (id INTEGER, category VARCHAR)")
    con.execute("INSERT INTO products VALUES (1, 'tools'), (2, 'toys')")
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, product_id INTEGER, amount DOUBLE)"
    )
    # Alice buys tools twice, Bob toys once; order 4 has no customer.
    con.execute(
        "INSERT INTO orders VALUES (1, 1, 1, 100), (2, 1, 1, 50), (3, 2, 2, 70), (4, NULL, 1, 5)"
    )
    return con


def _sql(query: dict) -> str:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    return CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    return sorted(
        (
            tuple(float(v) if isinstance(v, int | float) else v for v in row)
            for row in con.execute(sql).fetchall()
        ),
        key=str,
    )


def test_the_same_with_or_without_an_order_measure(con: duckdb.DuckDBPyConnection) -> None:
    alone = _rows(con, _sql({"select": {"measures": ["Customers Count", "Total Credit"]}}))
    beside = _rows(
        con, _sql({"select": {"measures": ["Revenue", "Customers Count", "Total Credit"]}})
    )
    assert alone == [(3.0, 70.0)]
    assert beside == [(225.0, 3.0, 70.0)]


def test_both_kinds_of_missing_link_are_rows(con: duckdb.DuckDBPyConnection) -> None:
    """Carol is in gold; the order without a customer is the NULL tier."""
    sql = _sql({"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Customers Count"]}})
    assert _rows(con, sql) == [
        ("gold", 150.0, 2.0),
        ("silver", 70.0, 1.0),
        (None, 5.0, 0.0),
    ]


def test_grouped_by_an_order_column(con: duckdb.DuckDBPyConnection) -> None:
    """Carol bought nothing, so she is the NULL category, and the credit adds up
    to what it reads alone (70)."""
    sql = _sql({"select": {"dimensions": ["Category"], "measures": ["Total Credit"]}})
    assert _rows(con, sql) == [("tools", 10.0), ("toys", 20.0), (None, 40.0)]


def test_a_where_no_such_customer_passes_leaves_them_out(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Carol's category is NULL, which ``tools`` rejects: no rows are added."""
    sql = _sql(
        {
            "select": {"dimensions": ["Category"], "measures": ["Total Credit"]},
            "where": [{"field": "Category", "op": "=", "value": "tools"}],
        }
    )
    assert "NOT EXISTS" not in sql
    assert _rows(con, sql) == [("tools", 10.0)]


def test_a_total_alone_keeps_the_null_group(con: duckdb.DuckDBPyConnection) -> None:
    """A total reads no group, so Carol's NULL category came from the plain
    credit measure beside it: alone, the total listed tools and toys only."""
    sql = _sql({"select": {"dimensions": ["Category"], "measures": ["All Credit"]}})
    assert _rows(con, sql) == [("tools", 70.0), ("toys", 70.0), (None, 70.0)]


def test_the_groups_do_not_depend_on_the_measures(con: duckdb.DuckDBPyConnection) -> None:
    def groups(measures: list[str]) -> list[object]:
        sql = _sql({"select": {"dimensions": ["Category"], "measures": measures}})
        return [row[0] for row in _rows(con, sql)]

    assert (
        groups(["All Credit"]) == groups(["Total Credit"]) == groups(["All Credit", "Total Credit"])
    )


def test_a_dimension_listed_twice_beside_a_total(con: duckdb.DuckDBPyConnection) -> None:
    """The group list is a union by name, which refuses a column named twice;
    a reaggregate's first stage lists a dimension as the query's and as its
    ``per``."""
    sql = _sql({"select": {"dimensions": ["Category", "Category"], "measures": ["All Credit"]}})
    assert [row[0] for row in _rows(con, sql)] == ["tools", "toys", None]


def test_a_total_adds_no_group_for_a_customer_the_where_rejects(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Carol is gold, so ``Tier = silver`` leaves her out, total or not."""
    query = {
        "select": {"dimensions": ["Category"], "measures": ["All Credit"]},
        "where": [{"field": "Tier", "op": "=", "value": "silver"}],
    }
    assert _rows(con, _sql(query)) == [("toys", 20.0)]


def test_one_cte_has_every_group() -> None:
    """The customers' CTE reads every order row and the customers without one,
    so it drives the query; no list of groups is collected from the others."""
    sql = _sql({"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Total Credit"]}})
    assert "__ob_groups" not in sql
    assert 'FROM "__ob_dedup_0" AS "__ob_dedup_0"\nLEFT JOIN "__ob_main"' in sql


def test_an_expression_cannot_give_a_missing_customer_a_value(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """``COALESCE(credit, 0) + 1`` is 1 over a NULL credit. Gating the column
    inside the aggregate still added that 1 for the order without a customer:
    the groups totalled 74 against 73 alone. The aggregate reads the rows that
    have a customer only; the order's group comes from the rows, valueless."""
    alone = _rows(con, _sql({"select": {"measures": ["Credit Plus One"]}}))
    grouped = _rows(
        con, _sql({"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Credit Plus One"]}})
    )
    assert alone == [(73.0,)]
    assert grouped == [("gold", 150.0, 52.0), ("silver", 70.0, 21.0), (None, 5.0, None)]


# --- ROLLUP / CUBE ------------------------------------------------------------
#
# Each grouping set is deduplicated on its own rows, which now include the
# customers without orders: the subtotals and the grand total count them, and
# their detail rows are the NULL category, as in a plain GROUP BY.


def test_rollup_totals_count_every_customer(con: duckdb.DuckDBPyConnection) -> None:
    """The grand total read 30, every customer with an order; it is 70."""
    sql = _sql(
        {"select": {"dimensions": ["Category"], "measures": ["Total Credit"]}, "grouping": "rollup"}
    )
    rows = con.execute(sql).fetchall()
    assert sorted(((r[0], float(r[1]), r[2]) for r in rows), key=str) == [
        ("tools", 10.0, 0),
        ("toys", 20.0, 0),
        (None, 40.0, 0),
        (None, 70.0, 1),
    ]


def test_rollup_subtotals_keep_both_null_groups(con: duckdb.DuckDBPyConnection) -> None:
    """Gold's subtotal is 50 (Alice and Carol), Carol is a detail row of her
    own, and the order without a customer keeps its revenue."""
    sql = _sql(
        {
            "select": {"dimensions": ["Tier", "Category"], "measures": ["Revenue", "Total Credit"]},
            "grouping": "rollup",
        }
    )
    assert _rows(con, sql) == [
        ("gold", "tools", 150.0, 10.0, 0.0, 0.0),
        ("gold", None, 150.0, 50.0, 0.0, 1.0),
        ("gold", None, None, 40.0, 0.0, 0.0),
        ("silver", "toys", 70.0, 20.0, 0.0, 0.0),
        ("silver", None, 70.0, 20.0, 0.0, 1.0),
        (None, "tools", 5.0, None, 0.0, 0.0),
        (None, None, 225.0, 70.0, 1.0, 1.0),
        (None, None, 5.0, None, 0.0, 1.0),
    ]


def test_cube_column_subtotals_count_every_customer(con: duckdb.DuckDBPyConnection) -> None:
    """The category subtotals over all tiers: tools has Alice's and the
    orphan's revenue with one customer, and Carol is the NULL category's."""
    sql = _sql(
        {
            "select": {
                "dimensions": ["Tier", "Category"],
                "measures": ["Revenue", "Total Credit", "Customers Count"],
            },
            "grouping": "cube",
        }
    )
    by_category = {
        r[1]: (r[2], r[3], r[4]) for r in con.execute(sql).fetchall() if r[5] == 1 and r[6] == 0
    }
    assert {
        k: tuple(None if v is None else float(v) for v in vs) for k, vs in by_category.items()
    } == {
        "tools": (155.0, 10.0, 1.0),
        "toys": (70.0, 20.0, 1.0),
        None: (None, 40.0, 1.0),
    }


def test_rollup_with_a_where_carol_passes(con: duckdb.DuckDBPyConnection) -> None:
    """``Category = tools OR Tier = gold`` holds for Carol: refused under
    ROLLUP before, now she is counted."""
    sql = _sql(
        {
            "select": {"dimensions": ["Category"], "measures": ["Total Credit"]},
            "grouping": "rollup",
            "where": [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Category", "op": "=", "value": "tools"},
                        {"field": "Tier", "op": "=", "value": "gold"},
                    ],
                }
            ],
        }
    )
    rows = con.execute(sql).fetchall()
    assert sorted(((r[0], float(r[1]), r[2]) for r in rows), key=str) == [
        ("tools", 10.0, 0),
        (None, 40.0, 0),
        (None, 50.0, 1),
    ]


def test_rollup_over_only_filtered_customer_measures_still_compiles(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """With every customer measure filtered on the orders there is no CTE to
    list the groups from. A filter no customer without orders passes adds
    nothing for them, so the plan stays as it was rather than refusing."""
    big_order_credit = """  Big Order Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Orders, column: Amount}
        operator: gt
        values: [{dataType: float, valueFloat: 90}]
"""
    yaml_text = MODEL_YAML.replace("metrics:\n", big_order_credit + "metrics:\n", 1)
    raw = YAML(typ="safe").load(yaml_text)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    query = QueryObject(
        **{
            "select": {"dimensions": ["Tier"], "measures": ["Revenue", "Big Order Credit"]},
            "grouping": "rollup",
        }
    )
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    by_set = {(r[0], r[3]): r[2] for r in con.execute(sql).fetchall()}
    assert float(by_set[("gold", 0)]) == 10.0
    assert by_set[("silver", 0)] is None
    assert float(by_set[(None, 1)]) == 10.0


@pytest.mark.parametrize("grouping", [None, "rollup"])
def test_a_metric_reads_a_missing_order_count_as_zero(
    con: duckdb.DuckDBPyConnection, grouping: str | None
) -> None:
    """Carol's group has no row of the order-side query: the order count she
    reads selected is 0, and the metric over it read NULL instead of 40."""
    query: dict = {
        "select": {
            "dimensions": ["Category"],
            "measures": ["Total Credit", "Orders Count", "Credit And Orders"],
        }
    }
    if grouping:
        query["grouping"] = grouping
    rows = con.execute(_sql(query)).fetchall()
    carol = [r[1:4] for r in rows if r[0] is None and (not grouping or r[4] == 0)]
    assert [tuple(float(v) for v in r) for r in carol] == [(40.0, 0.0, 40.0)]


@pytest.mark.parametrize("grouping", [None, "rollup", "cube"])
def test_a_metric_over_order_values_only_is_rebuilt(
    con: duckdb.DuckDBPyConnection, grouping: str | None
) -> None:
    """``{[Orders Count]} + 1`` reads no customer value, so it was computed
    whole by the order-side query, which has no row for Carol: NULL, though
    her order count is 0. It is rebuilt over that count: 1."""
    query: dict = {
        "select": {
            "dimensions": ["Tier", "Category"],
            "measures": ["Total Credit", "Orders Count", "Order Score"],
        }
    }
    if grouping:
        query["grouping"] = grouping
    rows = con.execute(_sql(query)).fetchall()
    carol = [
        tuple(float(v) for v in r[2:5])
        for r in rows
        if r[0] == "gold" and r[1] is None and (not grouping or r[6] == 0)
    ]
    assert carol == [(40.0, 0.0, 1.0)]
