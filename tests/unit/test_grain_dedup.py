"""Measures sourced from the *one* side of a forward many-to-one join.

``Sales`` joined to ``Products`` repeats each product row once per sale, so a
``SUM`` over a product column is inflated by that product's sale count.
``compiler/fanout.py`` treats a forward many-to-one as safe (true for measures
on the many side, false for the one side), so these queries used to compile to
silently overcounted SQL. ``compiler/grain_dedup.py`` now aggregates them over
deduplicated rows instead.

The arithmetic is pinned by executing the generated SQL against DuckDB with
data where the two answers differ.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.grain_dedup import GrainDedupUnsupportedError
from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.warnings import WarningCode
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
name: dedup_model

dataObjects:
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: string, primaryKey: true}
      List Price: {code: list_price, abstractType: float}
      Stock On Hand: {code: stock_on_hand, abstractType: int}
      Category: {code: category, abstractType: string}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: string, primaryKey: true}
      Age: {code: age, abstractType: int}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Sale Customer ID: {code: customer_id, abstractType: string}
      Region: {code: region, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
      Bumped Qty: {code: "", abstractType: int, expression: "{Quantity} + 1"}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Sale Customer ID]
        columnsTo: [Customer ID]

dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  Category: {dataObject: Products, column: Category, resultType: string}
  Bumped: {dataObject: Sales, column: Bumped Qty, resultType: int}

measures:
  Sold Quantity:
    resultType: int
    aggregation: sum
    expression: '{[Sales].[Quantity]}'
  Total Stock On Hand:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
  Highest List Price:
    resultType: float
    aggregation: max
    expression: '{[Products].[List Price]}'
  Distinct Prices:
    resultType: int
    aggregation: count_distinct
    expression: '{[Products].[List Price]}'
  Avg Customer Age:
    resultType: float
    aggregation: avg
    expression: '{[Customers].[Age]}'
  Product Count:
    columns:
      - dataObject: Products
        column: Product ID
    resultType: int
    aggregation: count
    distinct: true
  Product List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns:
      - dataObject: Products
        column: Product ID
    withinGroup:
      column: {dataObject: Sales, column: Quantity}
      order: ASC
  Ordered Product List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns:
      - dataObject: Products
        column: Product ID
    withinGroup:
      column: {dataObject: Products, column: Stock On Hand}
      order: ASC
  Big Sale Stock:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
    filters:
      - column: {dataObject: Sales, column: Quantity}
        operator: gt
        values: [{dataType: int, valueInt: 1}]
  Tools Stock:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
    filters:
      - column: {dataObject: Products, column: Category}
        operator: equals
        values: [{dataType: string, valueString: tools}]
  Sales Value:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Quantity]} * {[Products].[List Price]}'
  Total Stock On Hand Raw:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
    allowFanOut: true

metrics:
  Price per Unit:
    expression: '{[Total Stock On Hand]} / {[Sold Quantity]}'
  Quantity per Product:
    expression: '{[Sold Quantity]} / {[Product Count]}'
"""


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw = YAML(typ="safe").load(yaml_text)
    model, result = ReferenceResolver().resolve(raw)
    assert not result.errors, result.errors
    return model


def _compile(
    query: dict, yaml_text: str = MODEL_YAML, dialect: str = "duckdb"
) -> CompilationResult:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), dialect)


# --- The defect ---------------------------------------------------------


def test_one_side_measure_is_deduplicated_when_a_many_side_measure_rides_along() -> None:
    """The case that used to compile to an inflated SUM."""
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Total Stock On Hand"]}}
    )
    assert "dedup_0" in result.sql
    assert "SELECT DISTINCT" in result.sql
    # The product key is what makes the DISTINCT collapse replication rather
    # than merging two products that happen to share a price.
    assert '"Products"."id" AS "__ob_k0"' in result.sql


def test_deduplicated_measure_returns_the_uninflated_value() -> None:
    """Execute against DuckDB: the flattened join says 310, the truth is 210."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute(
        "INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'),"
        " ('p2', 9.99, 110, 'tools'), ('p3', 5.00, 300, 'parts')"
    )
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # p1 sells twice in north — the naive join counts its stock twice.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',2),"
        " ('s3','p2','c2','north',4), ('s4','p3','c2','south',8)"
    )

    flattened = con.execute(
        "SELECT s.region, SUM(s.quantity), SUM(p.stock_on_hand) FROM sales s"
        " LEFT JOIN products p ON s.product_id = p.id GROUP BY s.region ORDER BY 1"
    ).fetchall()
    assert flattened == [("north", 7, 310), ("south", 8, 300)]

    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "orderBy": [{"field": "Region"}],
        }
    )
    rows = [(r[0], int(r[1]), int(r[2])) for r in con.execute(result.sql).fetchall()]

    # Quantity is at sale grain and is untouched; stock now counts each
    # distinct product once per region: north = 100 + 110, south = 300.
    assert rows == [("north", 7, 210), ("south", 8, 300)]


def test_dedup_emits_a_fan_trap_warning_about_overlapping_groups() -> None:
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Total Stock On Hand"]}}
    )
    codes = [w.code for w in result.warnings]
    assert WarningCode.FAN_TRAP_RISK in codes
    message = next(w.message for w in result.warnings if w.code == WarningCode.FAN_TRAP_RISK)
    assert "do not add up" in message


def test_grain_anchored_count_does_not_count_unmatched_rows() -> None:
    """A LEFT JOIN miss is not one of the counted object's rows."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools')")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # 's2' points at a product that does not exist, so the join yields a row
    # whose whole product side is NULL.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','ghost','c1','north',5)"
    )

    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Products Count"]}}
    )
    rows = con.execute(result.sql).fetchall()

    # One real product, not two; quantity still spans both sales.
    assert [(r[0], int(r[1]), int(r[2])) for r in rows] == [("north", 6, 1)]


# --- Cases that must NOT be rewritten -----------------------------------


def test_measure_at_base_grain_is_untouched() -> None:
    result = _compile({"select": {"dimensions": ["Region"], "measures": ["Sold Quantity"]}})
    assert "dedup_0" not in result.sql
    assert "DISTINCT" not in result.sql


def test_measure_mixing_both_grains_is_untouched() -> None:
    """``quantity * list_price`` is evaluated per sale, so it is already correct."""
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Sales Value"]}}
    )
    assert "dedup_0" not in result.sql


@pytest.mark.parametrize("measure", ["Highest List Price", "Distinct Prices", "Product Count"])
def test_multiplicity_safe_aggregations_are_untouched(measure: str) -> None:
    """MAX and DISTINCT aggregates return the same answer over duplicated rows.

    ``Product Count`` is ``count`` + ``distinct: true`` over the joined
    object's key — counting parents from the child fact, the single most
    common one-side measure there is. Keying the safe-list on the
    ``aggregation`` string alone would wrongly rewrite it.
    """
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", measure]}}
    )
    assert "dedup_0" not in result.sql


def test_allow_fan_out_opts_out() -> None:
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand Raw"],
            }
        }
    )
    assert "dedup_0" not in result.sql
    assert 'SUM("Products"."stock_on_hand")' in result.sql


def test_dimension_only_query_is_untouched() -> None:
    result = _compile({"select": {"dimensions": ["Region", "Category"], "measures": []}})
    assert "dedup_0" not in result.sql


# --- Grain and projection details ---------------------------------------


def test_dedup_joins_back_on_every_query_dimension() -> None:
    result = _compile(
        {
            "select": {
                "dimensions": ["Region", "Category"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            }
        }
    )
    assert '"main"."Region" = "dedup_0"."Region"' in result.sql
    assert '"main"."Category" = "dedup_0"."Category"' in result.sql


def test_dedup_cross_joins_when_the_query_has_no_dimensions() -> None:
    result = _compile(
        {"select": {"dimensions": [], "measures": ["Sold Quantity", "Total Stock On Hand"]}}
    )
    assert "CROSS JOIN" in result.sql


def test_order_by_a_deduplicated_measure_targets_its_cte() -> None:
    """The measure's aggregate does not exist in the outer query's FROM."""
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "orderBy": [{"field": "Total Stock On Hand", "direction": "desc"}],
        }
    )
    assert 'ORDER BY "dedup_0"."Total Stock On Hand" DESC' in result.sql
    # The pre-wrap ORDER BY named the raw aggregate over a table the outer
    # query no longer selects from.
    assert "ORDER BY SUM(" not in result.sql
    assert '"Products"."stock_on_hand") DESC' not in result.sql


def test_order_by_a_dimension_targets_main() -> None:
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "orderBy": [{"field": "Region"}],
        }
    )
    assert '"main"."Region" ASC' in result.sql


def test_generated_sql_is_valid_for_every_dialect() -> None:
    query = {
        "select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Total Stock On Hand"]}
    }
    for dialect in (
        "bigquery",
        "clickhouse",
        "databricks",
        "dremio",
        "duckdb",
        "mysql",
        "postgres",
        "snowflake",
    ):
        result = _compile(query, dialect=dialect)
        assert result.sql_valid, (dialect, result.warnings)


# --- Combinations the rewrite refuses rather than getting wrong ---------


def test_total_measure_combined_with_dedup_is_refused() -> None:
    yaml_text = MODEL_YAML.replace(
        "  Sold Quantity:\n    resultType: int\n    aggregation: sum\n",
        "  Sold Quantity:\n    resultType: int\n    aggregation: sum\n    total: true\n",
    )
    with pytest.raises(GrainDedupUnsupportedError, match="deduplicated rows"):
        _compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Sold Quantity", "Total Stock On Hand"],
                }
            },
            yaml_text,
        )


def test_metric_over_a_deduplicated_component_is_refused() -> None:
    """A metric inlines its components, so it would read the inflated value."""
    with pytest.raises(GrainDedupUnsupportedError, match="Price per Unit"):
        _compile({"select": {"dimensions": ["Region"], "measures": ["Price per Unit"]}})


def test_rollup_combined_with_dedup_is_refused() -> None:
    with pytest.raises(GrainDedupUnsupportedError, match="grouping"):
        _compile(
            {
                "select": {
                    "dimensions": ["Region", "Category"],
                    "measures": ["Sold Quantity", "Total Stock On Hand"],
                },
                "grouping": "rollup",
            }
        )


def test_having_on_a_deduplicated_measure_is_refused() -> None:
    with pytest.raises(GrainDedupUnsupportedError, match="HAVING"):
        _compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Sold Quantity", "Total Stock On Hand"],
                },
                "having": [{"field": "Total Stock On Hand", "op": "gt", "value": 1}],
            }
        )


def test_refusals_are_catchable_as_fanout_errors() -> None:
    """Existing API handlers catch ``FanoutError``; the subclass keeps them working."""
    assert issubclass(GrainDedupUnsupportedError, FanoutError)


# --- Detection unit-level ------------------------------------------------


def test_replication_is_inherited_through_a_join_chain() -> None:
    """A measure two hops out on the one side is still replicated."""
    yaml_text = """
version: 1.0
name: chain
dataObjects:
  Suppliers:
    code: suppliers
    schema: main
    columns:
      Supplier ID: {code: id, abstractType: string, primaryKey: true}
      Rating: {code: rating, abstractType: float}
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: string, primaryKey: true}
      Product Supplier ID: {code: supplier_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Suppliers
        columnsFrom: [Product Supplier ID]
        columnsTo: [Supplier ID]
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: string, primaryKey: true}
      Age: {code: age, abstractType: int}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Sale Customer ID: {code: customer_id, abstractType: string}
      Region: {code: region, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
      Bumped Qty: {code: "", abstractType: int, expression: "{Quantity} + 1"}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Sale Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
measures:
  Sold Quantity:
    resultType: int
    aggregation: sum
    expression: '{[Sales].[Quantity]}'
  Total Rating:
    resultType: float
    aggregation: sum
    expression: '{[Suppliers].[Rating]}'
"""
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Total Rating"]}},
        yaml_text,
    )
    assert "dedup_0" in result.sql
    assert '"Suppliers"."id" AS "__ob_k0"' in result.sql


def test_distinct_aggregate_over_the_one_side_stays_correct() -> None:
    """A DISTINCT aggregate is unaffected by replication, so it is not rewritten.

    Executed rather than asserted on the SQL: two sales of ``p1`` in one region
    must still count one product.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools')")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    con.execute("INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',2)")

    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Product Count"]}}
    )
    assert "dedup_0" not in result.sql
    assert [(r[0], int(r[1]), int(r[2])) for r in con.execute(result.sql).fetchall()] == [
        ("north", 3, 1)
    ]


def test_having_on_a_distinct_one_side_measure_is_allowed() -> None:
    """The pattern the TPC-H quickstart uses: HAVING on a COUNT DISTINCT of the parent."""
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Product Count"]},
            "having": [{"field": "Product Count", "op": "gt", "value": 1}],
        }
    )
    assert "HAVING" in result.sql
    assert "dedup_0" not in result.sql


def test_metric_over_a_distinct_one_side_measure_is_allowed() -> None:
    result = _compile({"select": {"dimensions": ["Region"], "measures": ["Quantity per Product"]}})
    assert "dedup_0" not in result.sql


def test_average_over_the_one_side_is_unweighted_by_sale_count() -> None:
    """ "Average customer age per category sold" — an AVG over the joined object.

    The flattened join weights each customer by how many times they bought,
    which answers a different (and rarely intended) question. Deduplicating on
    the customer key averages each distinct buyer once.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute(
        "INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'), ('p2', 9.99, 110, 'tools')"
    )
    con.execute("CREATE TABLE customers (id VARCHAR, age INTEGER)")
    con.execute("INSERT INTO customers VALUES ('c1', 20), ('c2', 50)")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # c1 (20) buys three times, c2 (50) once — all in 'tools'.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',1),"
        " ('s3','p2','c1','north',1), ('s4','p2','c2','north',1)"
    )

    flattened = con.execute(
        "SELECT p.category, AVG(c.age) FROM sales s"
        " LEFT JOIN products p ON s.product_id = p.id"
        " LEFT JOIN customers c ON s.customer_id = c.id"
        " GROUP BY p.category"
    ).fetchone()
    # (20 + 20 + 20 + 50) / 4 — c1 counted once per purchase.
    assert flattened == ("tools", 27.5)

    result = _compile(
        {
            "select": {
                "dimensions": ["Category"],
                "measures": ["Sold Quantity", "Avg Customer Age"],
            }
        }
    )
    rows = con.execute(result.sql).fetchall()

    # (20 + 50) / 2 — each distinct buyer once.
    assert [(r[0], float(r[2])) for r in rows] == [("tools", 35.0)]


def test_a_one_side_measure_alone_is_still_refused_at_resolution() -> None:
    """The dedup rewrite needs a base-grain measure to anchor the query.

    With only a one-side measure, resolution anchors the base object on that
    measure's own source ('Customers'), from which the dimension's object
    ('Products') is unreachable — many-to-one joins are forward-only. The query
    is refused before planning, so the rewrite never sees it.

    Pinned here because it is the natural way to ask for "average customer age
    per category", and it does not work yet.
    """
    with pytest.raises(ResolutionError, match="cannot be reached from base"):
        _compile({"select": {"dimensions": ["Category"], "measures": ["Avg Customer Age"]}})


# --- Review findings on the first cut of this pass -----------------------


def test_filter_reaching_outside_the_dedup_grain_is_refused() -> None:
    """A measure filter compiles to CASE WHEN *inside* the aggregate.

    Its predicate columns must be projected for the CASE to evaluate, which puts
    them in the DISTINCT — so the rows collapse to one per (grain, product,
    predicate value) rather than one per (grain, product). A product with two
    qualifying sales at different quantities would be counted twice. Refuse
    rather than return that number.
    """
    with pytest.raises(GrainDedupUnsupportedError, match="filters clause references 'Sales'"):
        _compile(
            {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Big Sale Stock"]}}
        )


def test_filter_on_the_dedup_object_itself_still_deduplicates() -> None:
    """Predicate columns on the dedup object are fixed by its key, so DISTINCT is safe."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'), ('p2', 9.99, 7, 'parts')")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # p1 sells twice at different quantities — the case that breaks a filter
    # whose predicate reaches Sales.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',2), ('s2','p1','c1','north',3),"
        " ('s3','p2','c1','north',1)"
    )

    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Tools Stock"]}}
    )
    assert "dedup_0" in result.sql
    # Only p1 is 'tools', counted once despite two sales.
    assert [(r[0], int(r[2])) for r in con.execute(result.sql).fetchall()] == [("north", 100)]


def test_count_reads_zero_when_no_rows_match() -> None:
    """A group whose rows all miss the joined object contributes no dedup row.

    The LEFT JOIN then yields NULL, but COUNT over no rows is 0, not unknown.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools')")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # Every north sale points at a product that does not exist.
    con.execute(
        "INSERT INTO sales VALUES ('s1','ghost','c1','north',1), ('s2','ghost2','c1','north',1)"
    )

    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Products Count"]}}
    )
    assert "COALESCE" in result.sql
    assert [(r[0], int(r[2])) for r in con.execute(result.sql).fetchall()] == [("north", 0)]


def test_sum_stays_null_when_no_rows_match() -> None:
    """Only counts read zero — SUM over an empty input is NULL in SQL."""
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Total Stock On Hand"]}}
    )
    assert 'COALESCE("dedup_0"."Total Stock On Hand"' not in result.sql


def test_order_by_a_computed_dimension_targets_main() -> None:
    """A computed column's ORDER BY is a whole expression tree, not a ColumnRef.

    Left unmapped it would reference ``Sales`` in an outer query whose FROM is
    only the CTEs, and the database would fail to bind it.
    """
    result = _compile(
        {
            "select": {
                "dimensions": ["Region", "Bumped"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "orderBy": [{"field": "Bumped"}],
        }
    )
    assert 'ORDER BY "main"."Bumped" ASC' in result.sql
    assert '"Sales"."quantity" + 1 ASC' not in result.sql


def test_within_group_reaching_outside_the_dedup_grain_is_refused() -> None:
    """``withinGroup`` becomes the aggregate's ORDER BY, so its column is projected too.

    Same mechanism as an out-of-grain ``filters:`` predicate: the projected
    column joins the DISTINCT, so rows collapse to one per (grain, product,
    ordering value). A product with two sales at different quantities would be
    listed twice.
    """
    with pytest.raises(GrainDedupUnsupportedError, match="withinGroup clause references 'Sales'"):
        _compile(
            {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Product List"]}}
        )


def test_within_group_on_the_dedup_object_itself_still_deduplicates() -> None:
    """Ordering by a column of the deduplicated object is fixed by its key, so it is safe."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'), ('p2', 9.99, 7, 'parts')")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # p1 sells twice at different quantities — what breaks an out-of-grain order.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',2), ('s2','p1','c1','north',3),"
        " ('s3','p2','c1','north',1)"
    )

    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Ordered Product List"],
            }
        }
    )
    # Each product once, ordered by stock (p2=7 then p1=100) — not 'p2,p1,p1'.
    assert [r[2] for r in con.execute(result.sql).fetchall()] == ["p2,p1"]
