"""Measures on several parent objects keep each one's rows without children.

Beside an order measure, a customer measure and a product measure are both
read through the orders. Each object's rows without orders (a customer who
never ordered, a product never ordered) are added as a set of their own; the
query's groups are listed from every CTE, since none has them all. Before,
only one object could get such a set, and with two the plan stayed on the
order rows: Total Credit read 30 and Total Price 7 instead of 70 and 15.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

from .test_parents_without_children import MODEL_YAML as BASE_YAML

MODEL_YAML = BASE_YAML.replace(
    "      Category: {code: category, abstractType: string}\n",
    "      Category: {code: category, abstractType: string}\n"
    "      Price: {code: price, abstractType: float}\n",
).replace(
    "metrics:\n",
    """  Total Price:
    columns: [{dataObject: Products, column: Price}]
    aggregation: sum
    resultType: float
metrics:
""",
    1,
)

ALL = ["Revenue", "Total Credit", "Total Price"]
GAMES_OR_GOLD = [
    {
        "logic": "or",
        "filters": [
            {"field": "Category", "op": "=", "value": "games"},
            {"field": "Tier", "op": "=", "value": "gold"},
        ],
    }
]


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, tier VARCHAR, credit DOUBLE)")
    # Carol has no orders.
    con.execute("INSERT INTO customers VALUES (1, 'gold', 10), (2, 'silver', 20), (3, 'gold', 40)")
    con.execute("CREATE TABLE products (id INTEGER, category VARCHAR, price DOUBLE)")
    # Games is never ordered.
    con.execute("INSERT INTO products VALUES (1, 'tools', 3), (2, 'toys', 4), (3, 'games', 8)")
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


def _rows(con: duckdb.DuckDBPyConnection, query: dict) -> list[tuple[object, ...]]:
    return sorted(
        (
            tuple(float(v) if isinstance(v, int | float) else v for v in row)
            for row in con.execute(_sql(query)).fetchall()
        ),
        key=str,
    )


def test_each_parent_reads_as_it_does_alone(con: duckdb.DuckDBPyConnection) -> None:
    assert _rows(con, {"select": {"measures": ["Total Credit"]}}) == [(70.0,)]
    assert _rows(con, {"select": {"measures": ["Total Price"]}}) == [(15.0,)]
    assert _rows(con, {"select": {"measures": ALL}}) == [(225.0, 70.0, 15.0)]


def test_grouped_both_sets_are_rows(con: duckdb.DuckDBPyConnection) -> None:
    """Carol is gold; games, never ordered, is the NULL tier with the tools the
    order without a customer bought. A product reached from two tiers counts
    in both (tools: gold and NULL)."""
    assert _rows(con, {"select": {"dimensions": ["Tier"], "measures": ALL}}) == [
        ("gold", 150.0, 50.0, 3.0),
        ("silver", 70.0, 20.0, 4.0),
        (None, 5.0, None, 11.0),
    ]


def test_a_where_both_sets_can_pass(con: duckdb.DuckDBPyConnection) -> None:
    """``Category = games OR Tier = gold`` holds for Carol (gold, no category)
    and for games (no tier): read 10 and 3 before, without either."""
    query = {"select": {"measures": ALL}, "where": GAMES_OR_GOLD}
    assert _rows(con, query) == [(150.0, 50.0, 11.0)]


def test_under_rollup_several_parents_keep_the_plan() -> None:
    """ROLLUP reads its groups from one driving CTE, which several parents do
    not have: a WHERE asking for both sets is refused, not answered short."""
    with pytest.raises(ResolutionError) as raised:
        _sql(
            {
                "select": {"dimensions": ["Tier"], "measures": ALL},
                "grouping": "rollup",
                "where": GAMES_OR_GOLD,
            }
        )
    assert [e.code for e in raised.value.errors] == ["FILTER_ADMITS_ROWS_WITHOUT"]
