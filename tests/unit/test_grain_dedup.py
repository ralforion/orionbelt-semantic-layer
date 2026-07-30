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
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Region: {code: region, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]

dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  Category: {dataObject: Products, column: Category, resultType: string}

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
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, region VARCHAR, quantity INTEGER)"
    )
    # p1 sells twice in north — the naive join counts its stock twice.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','north',1), ('s2','p1','north',2),"
        " ('s3','p2','north',4), ('s4','p3','south',8)"
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
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, region VARCHAR, quantity INTEGER)"
    )
    # 's2' points at a product that does not exist, so the join yields a row
    # whose whole product side is NULL.
    con.execute("INSERT INTO sales VALUES ('s1','p1','north',1), ('s2','ghost','north',5)")

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


@pytest.mark.parametrize("measure", ["Highest List Price", "Distinct Prices"])
def test_multiplicity_safe_aggregations_are_untouched(measure: str) -> None:
    """MAX and COUNT DISTINCT return the same answer over duplicated rows."""
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
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Region: {code: region, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
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
