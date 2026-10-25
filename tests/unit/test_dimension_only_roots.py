"""A dimension-only query takes the combinations of every root joining its dimensions.

Orders and Returns both join Customers and Products. Region by category is the
pairs ordered *or* returned: one root picked by name answered "ordered pairs"
under ``Orders`` and "returned pairs" once the fact was renamed ``Sales``. Each
root is a CFL leg now, and the outer query groups the union.

=======  ========  ========  =======  ==========
fact     customer  region    product  category
=======  ========  ========  =======  ==========
orders   k1        North     p1       Books
orders   k2        South     p2       Games
orders   ``k9``    *(none)*  p1       Books
returns  k2        South     p1       Books
returns  k3        East      p2       Games
=======  ========  ========  =======  ==========
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: dimension_only_roots
dataObjects:
  Customers:
    code: customers
    columns:
      Cust Id: {code: id, abstractType: string, primaryKey: true}
      Region: {code: region, abstractType: string}
  Products:
    code: products
    columns:
      Product Id: {code: id, abstractType: string, primaryKey: true}
      Category: {code: category, abstractType: string}
  Calendar:
    code: calendar
    columns:
      Day: {code: d, abstractType: date, primaryKey: true}
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer_id, abstractType: string}
      Product: {code: product_id, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust Id]}
      - {joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product Id]}
      - {joinType: many-to-one, joinTo: Calendar, columnsFrom: [Order Date], columnsTo: [Day]}
  Returns:
    code: returns
    columns:
      Return Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer_id, abstractType: string}
      Product: {code: product_id, abstractType: string}
      Return Date: {code: return_date, abstractType: date}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [Cust Id]}
      - {joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product Id]}
      - {joinType: many-to-one, joinTo: Calendar, columnsFrom: [Return Date], columnsTo: [Day]}
dimensions:
  Region: {dataObject: Customers, column: Region, resultType: string}
  Category: {dataObject: Products, column: Category, resultType: string}
  Day: {dataObject: Calendar, column: Day, resultType: date}
  Order Date: {dataObject: Orders, column: Order Date, resultType: date}
"""

# Returns joining its order: its rows reach the customers through Orders, so
# Orders is the one root.
RETURNS_JOIN_ORDERS = """
      - {joinType: many-to-one, joinTo: Orders, columnsFrom: [Return Id], columnsTo: [Order Id]}
dimensions:"""

PAIRS = [
    ("East", "Games"),
    ("North", "Books"),
    ("South", "Books"),
    ("South", "Games"),
    (None, "Books"),
]


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customers AS SELECT * FROM"
        " (VALUES ('k1', 'North'), ('k2', 'South'), ('k3', 'East')) t(id, region)"
    )
    con.execute(
        "CREATE TABLE products AS SELECT * FROM"
        " (VALUES ('p1', 'Books'), ('p2', 'Games')) t(id, category)"
    )
    con.execute(
        "CREATE TABLE calendar AS SELECT d::DATE AS d"
        " FROM range(DATE '2025-01-01', DATE '2025-05-01', INTERVAL 1 DAY) t(d)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES"
        " ('o1', 'k1', 'p1', DATE '2025-01-05'), ('o2', 'k2', 'p2', DATE '2025-03-01'),"
        " ('o3', 'k9', 'p1', DATE '2025-03-02')) t(id, customer_id, product_id, order_date)"
    )
    con.execute(
        "CREATE TABLE returns AS SELECT * FROM (VALUES"
        " ('o1', 'k2', 'p1', DATE '2025-02-01'), ('o2', 'k3', 'p2', DATE '2025-02-02'))"
        " t(id, customer_id, product_id, return_date)"
    )
    return con


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _compile(query: dict, yaml_text: str = MODEL_YAML) -> CompilationResult:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), "duckdb")


def _rows(con: duckdb.DuckDBPyConnection, compiled: CompilationResult) -> list[tuple]:
    return sorted(con.execute(compiled.sql).fetchall(), key=str)


@pytest.mark.parametrize("orders_name", ["Orders", "Sales"])
def test_the_pairs_of_every_root_whatever_the_names(
    con: duckdb.DuckDBPyConnection, orders_name: str
) -> None:
    compiled = _compile(
        {"select": {"dimensions": ["Region", "Category"]}},
        MODEL_YAML.replace("Orders", orders_name),
    )

    assert compiled.explain is not None
    assert compiled.explain.planner == "CFL"
    assert '"Returns"' in compiled.explain.planner_reason
    assert f'"{orders_name}"' in compiled.explain.planner_reason
    assert _rows(con, compiled) == PAIRS


def test_a_root_reaching_another_is_not_a_leg(con: duckdb.DuckDBPyConnection) -> None:
    compiled = _compile(
        {"select": {"dimensions": ["Region", "Category"]}},
        MODEL_YAML.replace("\ndimensions:", RETURNS_JOIN_ORDERS, 1),
    )

    assert compiled.explain is not None
    assert compiled.explain.planner == "Star Schema"
    assert _rows(con, compiled) == [("North", "Books"), ("South", "Games"), (None, "Books")]


def test_a_filter_one_root_reaches_keeps_that_root(con: duckdb.DuckDBPyConnection) -> None:
    compiled = _compile(
        {
            "select": {"dimensions": ["Region", "Category"]},
            "where": [{"field": "Order Date", "op": ">=", "value": "2025-03-01"}],
        }
    )

    assert compiled.explain is not None
    assert compiled.explain.planner == "Star Schema"
    assert _rows(con, compiled) == [("South", "Games"), (None, "Books")]


def test_a_filter_every_root_reaches_applies_in_each_leg(
    con: duckdb.DuckDBPyConnection,
) -> None:
    compiled = _compile(
        {
            "select": {"dimensions": ["Region", "Category"]},
            "where": [{"field": "Region", "op": "equals", "value": "South"}],
        }
    )

    assert compiled.explain is not None
    assert compiled.explain.planner == "CFL"
    assert _rows(con, compiled) == [("South", "Books"), ("South", "Games")]


def test_dimensions_exclude_takes_away_the_pairs_of_every_root(
    con: duckdb.DuckDBPyConnection,
) -> None:
    compiled = _compile(
        {"select": {"dimensions": ["Region", "Category"]}, "dimensionsExclude": True}
    )

    # Three regions by two categories, less the four pairs ordered or returned.
    assert _rows(con, compiled) == [("East", "Books"), ("North", "Games")]


def test_rollup_over_the_union(con: duckdb.DuckDBPyConnection) -> None:
    compiled = _compile({"select": {"dimensions": ["Region", "Category"]}, "grouping": "rollup"})

    rows = con.execute(compiled.sql).fetchall()
    assert sorted((row[:2] for row in rows if row[2:] == (0, 0)), key=str) == PAIRS
    assert sum(1 for row in rows if row[2:] == (1, 1)) == 1


def test_fill_over_the_union(con: duckdb.DuckDBPyConnection) -> None:
    query = {
        "select": {"dimensions": ["Day:month", "Region"]},
        "where": [{"field": "Day", "op": "between", "value": ["2025-01-01", "2025-04-30"]}],
    }
    plain = _rows(con, _compile(query))
    filled = _rows(con, _compile({**query, "fill": True}))

    jan, feb, mar = date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)
    assert plain == sorted(
        [(jan, "North"), (feb, "East"), (feb, "South"), (mar, "South"), (mar, None)], key=str
    )
    # Four months, each with the three regions and the orphan order's NULL.
    assert len(filled) == 16
    assert set(plain) < set(filled)


def test_minimal_roots() -> None:
    graph = JoinGraph(_model())
    assert graph.minimal_roots({"Customers", "Products"}) == ["Orders", "Returns"]
    assert graph.minimal_roots({"Customers"}) == []
    assert graph.minimal_roots({"Orders", "Customers"}) == ["Orders"]

    linked = JoinGraph(_model(MODEL_YAML.replace("\ndimensions:", RETURNS_JOIN_ORDERS, 1)))
    assert linked.minimal_roots({"Customers", "Products"}) == ["Orders"]


def test_every_leg_is_a_cache_dependency() -> None:
    compiled = _compile({"select": {"dimensions": ["Region", "Category"]}})

    # A change to the returns has to invalidate the pairs it contributed.
    assert {"orders", "returns", "customers", "products"} <= set(compiled.physical_tables)


COMPUTED = (
    MODEL_YAML.replace(
        "  Customers:\n",
        """  Countries:
    code: countries
    columns:
      Country Id: {code: id, abstractType: string, primaryKey: true}
      Country Name: {code: name, abstractType: string}
  Customers:
""",
        1,
    ).replace(
        "      Region: {code: region, abstractType: string}\n",
        """      Region: {code: region, abstractType: string}
      Country: {code: country_id, abstractType: string}
      Country Upper: {expression: "upper({[Countries].[Country Name]})", abstractType: string}
    joins:
      - {joinType: many-to-one, joinTo: Countries, columnsFrom: [Country], columnsTo: [Country Id]}
""",
        1,
    )
    + "  Country Upper: {dataObject: Customers, column: Country Upper, resultType: string}\n"
)


@pytest.fixture
def con_countries(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    con.execute("CREATE TABLE countries AS SELECT * FROM (VALUES ('c1', 'italy')) t(id, name)")
    con.execute("ALTER TABLE customers ADD COLUMN country_id VARCHAR DEFAULT 'c1'")
    return con


def test_a_computed_dimension_joins_what_it_reads_in_each_leg(
    con_countries: duckdb.DuckDBPyConnection,
) -> None:
    compiled = _compile({"select": {"dimensions": ["Country Upper", "Category"]}}, COMPUTED)

    assert compiled.explain is not None
    assert compiled.explain.planner == "CFL"
    assert "returns" in compiled.physical_tables and "countries" in compiled.physical_tables
    # The orphan order's customer is unknown, so its pair has no country.
    assert _rows(con_countries, compiled) == [
        ("ITALY", "Books"),
        ("ITALY", "Games"),
        (None, "Books"),
    ]


def test_a_nested_root_keeps_the_single_base_that_unnests_it() -> None:
    nested = MODEL_YAML.replace(
        "  Orders:\n    code: orders\n",
        """  Order Batches:
    code: batches
    columns:
      Batch Id: {code: id, abstractType: string, primaryKey: true}
      Lines: {code: lines, abstractType: string}
  Orders:
    nestedIn: {dataObject: Order Batches, column: Lines}
""",
        1,
    )
    compiled = _compile({"select": {"dimensions": ["Region", "Category"]}}, nested)

    assert compiled.explain is not None
    assert compiled.explain.planner == "Star Schema"
    assert "UNNEST" in compiled.sql


def test_a_computed_dimension_beside_measures_of_two_facts(
    con_countries: duckdb.DuckDBPyConnection,
) -> None:
    compiled = _compile(
        {
            "select": {
                "dimensions": ["Country Upper"],
                "measures": ["Orders Count", "Returns Count"],
            }
        },
        COMPUTED,
    )

    assert compiled.explain is not None
    assert compiled.explain.planner == "CFL"
    assert _rows(con_countries, compiled) == [("ITALY", 2, 2), (None, 1, 0)]


STATIC_FILTER = """filters:
  - dataObject: Returns
    column: Return Date
    operator: gte
    value: '2025-02-02'
"""


@pytest.mark.parametrize("orders_name", ["Orders", "Sales"])
def test_a_static_filter_reaches_the_leg_of_its_root(
    con: duckdb.DuckDBPyConnection, orders_name: str
) -> None:
    # The filter leaves r2 (East, Games) and drops r1 (South, Books), in the
    # Returns leg whichever fact the base would have been.
    compiled = _compile(
        {"select": {"dimensions": ["Region", "Category"]}},
        (MODEL_YAML + STATIC_FILTER).replace("Orders", orders_name),
    )

    assert _rows(con, compiled) == [
        ("East", "Games"),
        ("North", "Books"),
        ("South", "Games"),
        (None, "Books"),
    ]


def test_dimensions_exclude_applies_a_static_filter_to_its_root_only(
    con: duckdb.DuckDBPyConnection,
) -> None:
    compiled = _compile(
        {"select": {"dimensions": ["Region", "Category"]}, "dimensionsExclude": True},
        MODEL_YAML + STATIC_FILTER,
    )

    # r1 (South, Books) is filtered out of the returns, so that pair is missing.
    assert _rows(con, compiled) == [("East", "Books"), ("North", "Games"), ("South", "Books")]
