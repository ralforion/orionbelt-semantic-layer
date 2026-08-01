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
  Grand Total Quantity:
    resultType: int
    aggregation: sum
    total: true
    expression: '{[Sales].[Quantity]}'
  Total Stock On Hand Raw:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
    allowFanOut: true
  Total List Price:
    resultType: float
    aggregation: sum
    expression: '{[Products].[List Price]}'
  Grand Total Stock:
    resultType: int
    aggregation: sum
    total: true
    expression: '{[Products].[Stock On Hand]}'

metrics:
  Price per Unit:
    expression: '{[Total Stock On Hand]} / {[Sold Quantity]}'
  Quantity per Product:
    expression: '{[Sold Quantity]} / {[Product Count]}'
  Stock per List Price:
    expression: '{[Total Stock On Hand]} / {[Total List Price]}'
  Stock Share:
    expression: '{[Total Stock On Hand]} / {[Grand Total Stock]}'
  Stock Rank:
    type: window
    windowFunction: dense_rank
    measure: Total Stock On Hand
    orderDirection: desc
  Doubled Stock Rank:
    expression: '{[Stock Rank]} * 2'
  Doubled Price per Unit:
    expression: '{[Price per Unit]} * 2'
  Stock per Grand Total:
    expression: '{[Total Stock On Hand]} / {[Grand Total Quantity]}'
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
    assert "__ob_dedup_0" in result.sql
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
    assert "__ob_dedup_0" not in result.sql
    assert "DISTINCT" not in result.sql


def test_measure_mixing_both_grains_is_untouched() -> None:
    """``quantity * list_price`` is evaluated per sale, so it is already correct."""
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity", "Sales Value"]}}
    )
    assert "__ob_dedup_0" not in result.sql


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
    assert "__ob_dedup_0" not in result.sql


def test_allow_fan_out_opts_out() -> None:
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand Raw"],
            }
        }
    )
    assert "__ob_dedup_0" not in result.sql
    assert 'SUM("Products"."stock_on_hand")' in result.sql


def test_dimension_only_query_is_untouched() -> None:
    result = _compile({"select": {"dimensions": ["Region", "Category"], "measures": []}})
    assert "__ob_dedup_0" not in result.sql


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
    assert '"__ob_main"."Region" = "__ob_dedup_0"."Region"' in result.sql
    assert '"__ob_main"."Category" = "__ob_dedup_0"."Category"' in result.sql


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
    assert 'ORDER BY "__ob_dedup_0"."Total Stock On Hand" DESC' in result.sql
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
    assert '"__ob_main"."Region" ASC' in result.sql


@pytest.mark.parametrize(
    "query",
    [
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            }
        },
        # A metric rebuilt over its deduplicated component.
        {"select": {"dimensions": ["Region"], "measures": ["Price per Unit"]}},
        # A HAVING predicate moved out to the wrapper's WHERE.
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "having": [{"field": "Total Stock On Hand", "op": "gt", "value": 250}],
        },
    ],
    ids=["measure", "metric", "having"],
)
def test_generated_sql_is_valid_for_every_dialect(query: dict) -> None:
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


def test_refusals_are_catchable_as_fanout_errors() -> None:
    """Existing API handlers catch ``FanoutError``; the subclass keeps them working."""
    assert issubclass(GrainDedupUnsupportedError, FanoutError)


# --- Metrics over deduplicated components --------------------------------


def _sales_db() -> duckdb.DuckDBPyConnection:
    """Three products, four sales — p1 sells twice in the north region.

    The flattened join reads 310 stock in the north; deduplicated it reads 210.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute(
        "INSERT INTO products VALUES ('p1', 10.0, 100, 'tools'),"
        " ('p2', 20.0, 110, 'tools'), ('p3', 5.0, 300, 'parts')"
    )
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',2),"
        " ('s3','p2','c2','north',4), ('s4','p3','c2','south',8)"
    )
    return con


def test_metric_component_is_computed_from_the_deduplicated_value() -> None:
    """The component moves into a dedup CTE; the metric is rebuilt over the results."""
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Price per Unit"]},
            "orderBy": [{"field": "Region"}],
        }
    )
    # The component's aggregate is no longer inlined into the metric column.
    assert 'SUM("Products"."stock_on_hand") / SUM' not in result.sql
    assert '"__ob_dedup_0"."Total Stock On Hand" / "__ob_main"."Sold Quantity"' in result.sql

    rows = [(r[0], float(r[1])) for r in _sales_db().execute(result.sql).fetchall()]
    # north: 210 / 7, not the flattened 310 / 7. south: 300 / 8.
    assert rows == [("north", 30.0), ("south", 37.5)]


def test_metric_over_a_deduplicated_component_keeps_its_declared_cast() -> None:
    result = _compile({"select": {"dimensions": ["Region"], "measures": ["Price per Unit"]}})
    assert 'AS DECIMAL(18, 6)) AS "Price per Unit"' in result.sql


def test_a_component_also_selected_on_its_own_is_computed_once() -> None:
    """The metric reads the column the selected measure already put in the CTE."""
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand", "Price per Unit"],
            },
            "orderBy": [{"field": "Region"}],
        }
    )
    # One aggregate each, read twice: once for the measure, once by the metric.
    assert result.sql.count('SUM("__ob_dedup_src_0"."__ob_c0")') == 1
    assert result.sql.count('SUM("Sales"."quantity")') == 1

    rows = [
        (r[0], int(r[1]), int(r[2]), float(r[3]))
        for r in _sales_db().execute(result.sql).fetchall()
    ]
    assert rows == [("north", 7, 210, 30.0), ("south", 8, 300, 37.5)]


def test_total_on_another_measure_composes_with_a_split_metric() -> None:
    """The totals wrapper reads the rebuilt metric column by alias, like any other."""
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Grand Total Quantity", "Price per Unit"],
            },
            "orderBy": [{"field": "Region"}],
        }
    )
    assert 'SUM("Grand Total Quantity") OVER ()' in result.sql

    rows = [(r[0], int(r[1]), float(r[2])) for r in _sales_db().execute(result.sql).fetchall()]
    # The grand total spans both regions; the metric stays per-region.
    assert rows == [("north", 15, 30.0), ("south", 15, 37.5)]


def test_two_deduplicated_components_share_one_dedup_cte() -> None:
    """Both components are sourced from the same replicated object."""
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Stock per List Price"]},
            "orderBy": [{"field": "Region"}],
        }
    )
    assert "__ob_dedup_1" not in result.sql

    rows = [(r[0], float(r[1])) for r in _sales_db().execute(result.sql).fetchall()]
    # north: 210 stock / 30.0 list price (p1 and p2 counted once each), south: 300 / 5.
    assert rows == [("north", 7.0), ("south", 60.0)]


def test_order_by_a_metric_over_a_deduplicated_component() -> None:
    """The metric is computed in the outer projection, so it sorts by its alias."""
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Price per Unit"]},
            "orderBy": [{"field": "Price per Unit", "direction": "desc"}],
        }
    )
    assert 'ORDER BY "Price per Unit" DESC' in result.sql
    rows = [r[0] for r in _sales_db().execute(result.sql).fetchall()]
    assert rows == ["south", "north"]


def test_a_nested_derived_metric_splits_the_same_way() -> None:
    """A derived metric over a derived metric is inlined into one expression.

    So its leaves face the same replicating join, and the split has to follow
    the nesting to reach them.
    """
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Doubled Price per Unit"]},
            "orderBy": [{"field": "Region"}],
        }
    )
    assert '"__ob_dedup_0"."Total Stock On Hand" / "__ob_main"."Sold Quantity"' in result.sql

    rows = [(r[0], float(r[1])) for r in _sales_db().execute(result.sql).fetchall()]
    # Twice the per-region price per unit: 30 and 37.5 deduplicated.
    assert rows == [("north", 60.0), ("south", 75.0)]


def test_metric_reaching_a_deduplicated_measure_through_a_wrapper_metric_is_refused() -> None:
    """A derived metric over a *window* metric over a deduplicated measure.

    The window wrapper projects the window metric's base measure as a column of
    its base CTE, rebuilt from the fact tables, which the split cannot supply
    from a dedup CTE — so the query is refused rather than answered from the
    inflated value.
    """
    with pytest.raises(GrainDedupUnsupportedError, match="Stock Rank"):
        _compile({"select": {"dimensions": ["Region"], "measures": ["Doubled Stock Rank"]}})


def test_total_on_a_deduplicated_metric_component_is_refused() -> None:
    """The totals wrapper re-projects the component's raw aggregate.

    Its base CTE reads from the dedup output, where the fact tables are gone —
    so the combination is refused rather than emitted.
    """
    with pytest.raises(GrainDedupUnsupportedError, match="totals"):
        _compile({"select": {"dimensions": ["Region"], "measures": ["Stock Share"]}})


def test_total_on_any_component_of_a_split_metric_is_refused() -> None:
    """The total need not be on the deduplicated component to conflict.

    Once a metric is split across dedup CTEs, the totals wrapper decomposes it
    again and re-projects *every* component's raw aggregate into a base CTE
    whose FROM is the dedup output - so a ``total: true`` sibling of a
    deduplicated component breaks it just as badly.
    """
    with pytest.raises(GrainDedupUnsupportedError, match="totals"):
        _compile({"select": {"dimensions": ["Region"], "measures": ["Stock per Grand Total"]}})


def test_period_over_period_over_a_deduplicated_component_is_refused() -> None:
    yaml_text = (
        CUMULATIVE_YAML
        + """  Stock Growth:
    type: period_over_period
    expression: '{[Total Stock On Hand]}'
    periodOverPeriod:
      timeDimension: Sale Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""
    )
    with pytest.raises(GrainDedupUnsupportedError, match="period_over_period"):
        _compile(
            {"select": {"dimensions": ["Sale Month"], "measures": ["Stock Growth"]}},
            yaml_text,
        )


# --- HAVING on deduplicated measures -------------------------------------


def test_having_on_a_deduplicated_measure_filters_the_outer_query() -> None:
    """The wrapper's own query is one row per query grain, so HAVING moves there."""
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "having": [{"field": "Total Stock On Hand", "op": "gt", "value": 250}],
        }
    )
    assert 'WHERE "__ob_dedup_0"."Total Stock On Hand" > 250' in result.sql
    assert "HAVING" not in result.sql

    rows = [(r[0], int(r[1]), int(r[2])) for r in _sales_db().execute(result.sql).fetchall()]
    # north's deduplicated stock is 210 — the flattened 310 would have passed.
    assert rows == [("south", 8, 300)]


def test_having_only_deduplicated_measure_stays_out_of_the_projection() -> None:
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Sold Quantity"]},
            "having": [{"field": "Total Stock On Hand", "op": "gt", "value": 250}],
        }
    )
    rows = _sales_db().execute(result.sql).fetchall()
    assert [(r[0], int(r[1])) for r in rows] == [("south", 8)]
    assert all(len(r) == 2 for r in rows)


def test_having_splits_between_the_grouped_query_and_the_wrapper() -> None:
    """A base-grain predicate stays a HAVING; the deduplicated one moves out."""
    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            },
            "having": [
                {"field": "Sold Quantity", "op": "gt", "value": 5},
                {"field": "Total Stock On Hand", "op": "gt", "value": 250},
            ],
        }
    )
    assert 'HAVING CAST(SUM("Sales"."quantity") AS DECIMAL(18, 2)) > 5' in result.sql
    assert 'WHERE "__ob_dedup_0"."Total Stock On Hand" > 250' in result.sql

    rows = [(r[0], int(r[1]), int(r[2])) for r in _sales_db().execute(result.sql).fetchall()]
    assert rows == [("south", 8, 300)]


def test_having_on_a_metric_over_a_deduplicated_component() -> None:
    """The metric only exists in the outer projection, so its predicate goes there too."""
    result = _compile(
        {
            "select": {"dimensions": ["Region"], "measures": ["Price per Unit"]},
            "having": [{"field": "Price per Unit", "op": "gt", "value": 31}],
        }
    )
    rows = [(r[0], float(r[1])) for r in _sales_db().execute(result.sql).fetchall()]
    assert rows == [("south", 37.5)]


def test_having_mixing_a_dimension_with_a_deduplicated_measure_is_refused() -> None:
    """The moved predicate would carry a physical column with nothing to bind to."""
    with pytest.raises(GrainDedupUnsupportedError, match="'Region'"):
        _compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Sold Quantity", "Total Stock On Hand"],
                },
                "having": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Region", "op": "equals", "value": "north"},
                            {"field": "Total Stock On Hand", "op": "gt", "value": 250},
                        ],
                    }
                ],
            }
        )


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
    assert "__ob_dedup_0" in result.sql
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
    assert "__ob_dedup_0" not in result.sql
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
    assert "__ob_dedup_0" not in result.sql


def test_metric_over_a_distinct_one_side_measure_is_allowed() -> None:
    result = _compile({"select": {"dimensions": ["Region"], "measures": ["Quantity per Product"]}})
    assert "__ob_dedup_0" not in result.sql


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


def test_a_one_side_measure_alone_reanchors_and_deduplicates() -> None:
    """ "Average customer age per category" asked on its own, with no other measure.

    Anchoring the base on the measure's own source picks 'Customers', which
    reaches nothing — so this used to fail with UNREACHABLE_REQUIRED_OBJECT.
    Resolution now re-anchors on the common root ('Sales'), and the measure is
    deduplicated exactly as it is when a sale-grain measure rides along.
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
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',1),"
        " ('s3','p2','c1','north',1), ('s4','p2','c2','north',1)"
    )

    result = _compile({"select": {"dimensions": ["Category"], "measures": ["Avg Customer Age"]}})

    assert "__ob_dedup_0" in result.sql
    assert [(r[0], float(r[1])) for r in con.execute(result.sql).fetchall()] == [("tools", 35.0)]


def test_reanchoring_does_not_fire_for_multi_fact_queries() -> None:
    """CFL detection runs on the base object, so multi-fact keeps its original base."""
    yaml_text = """
version: 1.0
name: twofact
dataObjects:
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: string, primaryKey: true}
      Country: {code: country, abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: string, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: string}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
  Shipments:
    code: shipments
    schema: main
    columns:
      Shipment ID: {code: id, abstractType: string, primaryKey: true}
      Shipment Customer ID: {code: customer_id, abstractType: string}
      Weight: {code: weight, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Shipment Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Country: {dataObject: Customers, column: Country, resultType: string}
measures:
  Order Amount:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Amount]}'
  Shipment Weight:
    resultType: float
    aggregation: sum
    expression: '{[Shipments].[Weight]}'
"""
    result = _compile(
        {"select": {"dimensions": ["Country"], "measures": ["Order Amount", "Shipment Weight"]}},
        yaml_text,
    )
    assert result.explain is not None
    assert result.explain.planner == "CFL"
    assert "UNION ALL" in result.sql


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
    assert "__ob_dedup_0" in result.sql
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
    assert 'COALESCE("__ob_dedup_0"."Total Stock On Hand"' not in result.sql


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
    assert 'ORDER BY "__ob_main"."Bumped" ASC' in result.sql
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


def test_where_on_an_unreachable_object_re_anchors_instead_of_being_dropped() -> None:
    """A WHERE filter must not be silently discarded.

    WHERE filters resolve long after the base object is chosen, so the objects
    they name have to be collected up front — otherwise the base stays on the
    measure's own source, the filter's object is unreachable from it, and the
    filter is dropped downstream as "irrelevant to this query". The query then
    answers a different question than the one asked, with no error.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'), ('p2', 9.99, 7, 'parts')")
    con.execute("CREATE TABLE customers (id VARCHAR, age INTEGER)")
    con.execute("INSERT INTO customers VALUES ('c1', 20), ('c2', 50), ('c3', 90)")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # c1 and c2 buy tools; c3 buys parts only.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',1),"
        " ('s3','p1','c2','north',1), ('s4','p2','c3','north',1)"
    )

    result = _compile(
        {
            "select": {"dimensions": [], "measures": ["Avg Customer Age"]},
            "where": [{"field": "Category", "op": "=", "value": "tools"}],
        }
    )
    assert "category" in result.sql  # the filter survived into the SQL
    # Tools buyers are c1 (20) and c2 (50) — not the unfiltered (20+50+90)/3.
    assert [float(r[0]) for r in con.execute(result.sql).fetchall()] == [35.0]


def test_scalar_query_with_only_deduplicated_measures_returns_one_row() -> None:
    """With no dimensions and every measure deduplicated, ``main`` projects nothing.

    Left in place it degenerates to ``SELECT *`` over the base rows and
    multiplies the single scalar result, so it is dropped and the dedup CTE
    stands alone. The WHERE is what forces the join here — without it the
    measure is queried at its own grain and no dedup is needed.
    """
    result = _compile(
        {
            "select": {"dimensions": [], "measures": ["Avg Customer Age"]},
            "where": [{"field": "Category", "op": "=", "value": "tools"}],
        }
    )
    assert "__ob_dedup_0" in result.sql
    assert 'FROM "__ob_main" AS "__ob_main"' not in result.sql
    assert "SELECT\n  *" not in result.sql

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools')")
    con.execute("CREATE TABLE customers (id VARCHAR, age INTEGER)")
    con.execute("INSERT INTO customers VALUES ('c1', 20), ('c2', 60)")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # c1 buys three times, c2 once — four base rows, one scalar answer.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',1),"
        " ('s3','p1','c1','north',1), ('s4','p1','c2','north',1)"
    )
    assert [float(r[0]) for r in con.execute(result.sql).fetchall()] == [40.0]


def test_static_model_filters_do_not_drive_re_anchoring() -> None:
    """Static filters are declared "apply where relevant", unlike a query WHERE.

    They must not pull the base towards a table the query never mentioned.
    """
    yaml_text = (
        MODEL_YAML
        + """
filters:
  - dataObject: Products
    column: Category
    operator: equals
    value: tools
"""
    )
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sold Quantity"]}}, yaml_text
    )
    assert result.resolved.fact_tables == ["Sales"]


def test_where_on_a_disconnected_object_is_still_skipped_not_crashed() -> None:
    """Re-anchoring must not fire when nothing can reach the filter's object.

    ``find_common_root`` falls back to an undirected Steiner centre, which is a
    best effort rather than a guarantee — and used to raise ``NetworkXNoPath``
    outright when the required objects spanned disconnected components. The base
    stays put and the filter keeps its existing "irrelevant to this query" skip.
    """
    yaml_text = """
version: 1.0
name: disconnected
dataObjects:
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: string, primaryKey: true}
      Category: {code: category, abstractType: string}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
  Suppliers:
    code: suppliers
    schema: main
    columns:
      Supplier ID: {code: id, abstractType: string, primaryKey: true}
      Supplier Name: {code: name, abstractType: string}
dimensions:
  Category: {dataObject: Products, column: Category, resultType: string}
  Supplier Name: {dataObject: Suppliers, column: Supplier Name, resultType: string}
measures:
  Sold Quantity:
    resultType: int
    aggregation: sum
    expression: '{[Sales].[Quantity]}'
"""
    result = _compile(
        {
            "select": {"dimensions": ["Category"], "measures": ["Sold Quantity"]},
            "where": [{"field": "Supplier Name", "op": "=", "value": "Acme"}],
        },
        yaml_text,
    )
    assert result.resolved.fact_tables == ["Sales"]
    assert "suppliers" not in result.sql


def test_total_on_a_base_grain_measure_composes_with_dedup() -> None:
    """``total`` only conflicts when it sits on a deduplicated measure.

    The totals pass runs after dedup and wraps its output in a ``base`` CTE, so
    a total on a base-grain measure never reaches into the dedup CTE. Refusing
    the whole query because some *other* measure carries ``total`` rejected work
    that composes correctly.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute(
        "INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'),"
        " ('p2', 9.99, 110, 'tools'), ('p3', 5.0, 300, 'parts')"
    )
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',2),"
        " ('s3','p2','c2','north',4), ('s4','p3','c2','south',8)"
    )

    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Grand Total Quantity", "Total Stock On Hand"],
            }
        }
    )
    assert "__ob_dedup_0" in result.sql
    assert "OVER ()" in result.sql

    rows = sorted((r[0], int(r[1]), int(r[2])) for r in con.execute(result.sql).fetchall())
    # The total is the window over every row (1+2+4+8); the stock is still
    # deduplicated per region, p1 counted once despite two sales.
    assert rows == [("north", 15, 210), ("south", 15, 300)]


def test_total_on_the_deduplicated_measure_uses_a_scalar_grain_dedup_cte() -> None:
    """``total: true`` on a deduplicated measure means "each source row once, overall".

    It cannot be a window over this pass's output: those per-group values belong
    to overlapping groups — a product sold in two regions is legitimately in
    both — so ``SUM(...) OVER ()`` would double count. The measure is instead
    aggregated in its own CTE deduplicated at *no* grain.
    """
    yaml_text = MODEL_YAML.replace(
        "  Total Stock On Hand:\n    resultType: int\n    aggregation: sum\n",
        "  Total Stock On Hand:\n    resultType: int\n    aggregation: sum\n    total: true\n",
    )
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute(
        "INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'),"
        " ('p2', 9.99, 110, 'tools'), ('p3', 5.0, 300, 'parts')"
    )
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # p1 sells in BOTH regions, so it belongs to two groups.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p2','c1','north',1),"
        " ('s3','p1','c1','south',1), ('s4','p3','c1','south',1)"
    )

    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Sold Quantity", "Total Stock On Hand"],
            }
        },
        yaml_text,
    )
    assert "__ob_dedup_total_0" in result.sql
    assert "OVER ()" not in result.sql

    rows = sorted((r[0], int(r[2])) for r in con.execute(result.sql).fetchall())
    # 100 + 110 + 300, each product once. Summing the per-group values
    # (north 210 + south 400) would give 610.
    assert rows == [("north", 510), ("south", 510)]


def test_grain_override_on_a_deduplicated_measure_is_still_refused() -> None:
    """Unlike ``total``, a grain override has no dedup CTE built for its grain yet."""
    yaml_text = MODEL_YAML.replace(
        "  Total Stock On Hand:\n    resultType: int\n    aggregation: sum\n",
        "  Total Stock On Hand:\n    resultType: int\n    aggregation: sum\n"
        "    grain:\n      mode: FIXED\n      keepOnly: [Region]\n",
    )
    with pytest.raises(GrainDedupUnsupportedError, match="totals"):
        _compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Sold Quantity", "Total Stock On Hand"],
                }
            },
            yaml_text,
        )


def test_deduplicated_total_is_not_rewrapped_when_another_total_is_present() -> None:
    """The totals pass must treat dedup-handled measures as pass-through throughout.

    Excluding them only while collecting names is not enough: the outer
    projection re-tests each measure, so as soon as *another* measure pulls the
    totals pass in, the deduplicated scalar gets a second
    ``SUM(...) OVER ()`` wrapped around it — once per group row.
    """
    yaml_text = MODEL_YAML.replace(
        "  Total Stock On Hand:\n    resultType: int\n    aggregation: sum\n",
        "  Total Stock On Hand:\n    resultType: int\n    aggregation: sum\n    total: true\n",
    )
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute(
        "INSERT INTO products VALUES ('p1', 9.99, 100, 'tools'),"
        " ('p2', 9.99, 110, 'tools'), ('p3', 5.0, 300, 'parts')"
    )
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p2','c1','north',1),"
        " ('s3','p1','c1','south',1), ('s4','p3','c1','south',1)"
    )

    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Grand Total Quantity", "Total Stock On Hand"],
            }
        },
        yaml_text,
    )
    # The base-grain total is a window; the deduplicated one is projected as-is.
    assert 'SUM("Grand Total Quantity") OVER ()' in result.sql
    assert 'SUM("Total Stock On Hand") OVER ()' not in result.sql

    rows = sorted((r[0], int(r[1]), int(r[2])) for r in con.execute(result.sql).fetchall())
    # Stock is 510 (each product once), not 1020 (510 doubled by the second wrap).
    assert rows == [("north", 4, 510), ("south", 4, 510)]


def test_deduplicated_avg_total_alongside_another_total() -> None:
    """AVG totals take a separate path with sum/count helper columns.

    A deduplicated one must skip that path too, or the helpers are emitted for a
    measure whose value never passes through the base CTE.
    """
    yaml_text = MODEL_YAML.replace(
        "  Avg Customer Age:\n    resultType: float\n    aggregation: avg\n",
        "  Avg Customer Age:\n    resultType: float\n    aggregation: avg\n    total: true\n",
    )
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE products (id VARCHAR, list_price DOUBLE,"
        " stock_on_hand INTEGER, category VARCHAR)"
    )
    con.execute("INSERT INTO products VALUES ('p1', 9.99, 100, 'tools')")
    con.execute("CREATE TABLE customers (id VARCHAR, age INTEGER)")
    con.execute("INSERT INTO customers VALUES ('c1', 20), ('c2', 60)")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, customer_id VARCHAR,"
        " region VARCHAR, quantity INTEGER)"
    )
    # c1 buys three times, c2 once — weighting by purchase would give 30, not 40.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','c1','north',1), ('s2','p1','c1','north',1),"
        " ('s3','p1','c1','north',1), ('s4','p1','c2','south',1)"
    )

    result = _compile(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Grand Total Quantity", "Avg Customer Age"],
            }
        },
        yaml_text,
    )
    assert "__sum" not in result.sql and "__count" not in result.sql
    rows = sorted((r[0], float(r[2])) for r in con.execute(result.sql).fetchall())
    assert rows == [("north", 40.0), ("south", 40.0)]


# --- composing with the aggregate-mode wrappers ---------------------------

CUMULATIVE_YAML = """\
version: 1.0
name: cw
dataObjects:
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: string, primaryKey: true}
      Stock On Hand: {code: stock_on_hand, abstractType: int}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Sale Date: {code: sale_date, abstractType: date}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
dimensions:
  Sale Month: {dataObject: Sales, column: Sale Date, resultType: date, timeGrain: month}
measures:
  Sold Quantity:
    resultType: int
    aggregation: sum
    dataType: "decimal(18, 2)"
    expression: '{[Sales].[Quantity]}'
  Total Stock On Hand:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
metrics:
  Running Quantity:
    type: cumulative
    measure: Sold Quantity
    timeDimension: Sale Month
  Quantity Rank:
    type: window
    windowFunction: dense_rank
    measure: Sold Quantity
    orderDirection: desc
"""


def _cumulative_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE products (id VARCHAR, stock_on_hand INTEGER)")
    con.execute("INSERT INTO products VALUES ('p1', 100), ('p2', 110)")
    con.execute(
        "CREATE TABLE sales (id VARCHAR, product_id VARCHAR, sale_date DATE, quantity INTEGER)"
    )
    # p1 sells twice in January — the replication the dedup pass removes.
    con.execute(
        "INSERT INTO sales VALUES ('s1','p1','2024-01-05',1), ('s2','p1','2024-01-20',2),"
        " ('s3','p2','2024-02-10',4)"
    )
    return con


def test_cumulative_metric_composes_with_a_deduplicated_measure() -> None:
    """The wrapper must take the base measure by alias, not rebuild its aggregate.

    Re-deriving ``SUM("Sales"."quantity")`` into a CTE whose FROM is the dedup
    output fails to bind: the fact tables are no longer in scope there.
    """
    result = _compile(
        {
            "select": {
                "dimensions": ["Sale Month"],
                "measures": ["Running Quantity", "Total Stock On Hand"],
            }
        },
        CUMULATIVE_YAML,
    )
    # Taken by alias from the dedup output, wrapped in the measure's declared cast.
    assert '"__ob_main"."Running Quantity"' in result.sql
    assert 'AS "Sold Quantity"' in result.sql
    assert 'SUM("Sales"."quantity")' not in result.sql.split("cumulative_base")[-1]

    rows = _cumulative_db().execute(result.sql).fetchall()
    # Cumulative 1+2=3 then +4=7; stock counts p1 once in January despite two sales.
    assert [(int(r[1]), int(r[2])) for r in rows] == [(3, 100), (7, 110)]


def test_window_metric_composes_with_a_deduplicated_measure() -> None:
    result = _compile(
        {
            "select": {
                "dimensions": ["Sale Month"],
                "measures": ["Quantity Rank", "Total Stock On Hand"],
            }
        },
        CUMULATIVE_YAML,
    )
    assert "dense_rank" in result.sql.lower()

    rows = _cumulative_db().execute(result.sql).fetchall()
    # February (qty 4) outranks January (qty 3); stock stays deduplicated.
    assert sorted((int(r[1]), int(r[2])) for r in rows) == [(1, 110), (2, 100)]


_DEDUPLICATED_BASE_YAML = (
    CUMULATIVE_YAML
    + """  Running Stock:
    type: cumulative
    measure: Total Stock On Hand
    timeDimension: Sale Month
  Stock Rank:
    type: window
    windowFunction: dense_rank
    measure: Total Stock On Hand
    orderDirection: desc
"""
)


def test_cumulative_over_a_deduplicated_base_measure() -> None:
    """The base measure is the deduplicated one, so it is split into its own CTE.

    The wrapper then windows over that column by alias, exactly as it does for a
    base measure the planner left in place.
    """
    result = _compile(
        {"select": {"dimensions": ["Sale Month"], "measures": ["Running Stock"]}},
        _DEDUPLICATED_BASE_YAML,
    )
    assert '"__ob_dedup_0"."Total Stock On Hand"' in result.sql

    rows = [(int(r[1])) for r in _cumulative_db().execute(result.sql).fetchall()]
    # January counts p1's stock once despite two sales: 100, then +110.
    assert rows == [100, 210]


def test_window_over_a_deduplicated_base_measure() -> None:
    result = _compile(
        {"select": {"dimensions": ["Sale Month"], "measures": ["Stock Rank"]}},
        _DEDUPLICATED_BASE_YAML,
    )
    rows = sorted(int(r[1]) for r in _cumulative_db().execute(result.sql).fetchall())
    # February's 110 outranks January's deduplicated 100.
    assert rows == [1, 2]


def test_filter_context_with_a_deduplicated_measure_is_still_refused() -> None:
    """filterContext re-queries the fact tables under a *different* WHERE.

    It cannot read the dedup output, which has already applied the query's
    filters and aggregated, so unlike cumulative and window there is no column
    to take by alias.
    """
    yaml_text = CUMULATIVE_YAML.replace(
        "  Total Stock On Hand:\n",
        "  Unfiltered Quantity:\n"
        "    resultType: int\n"
        "    aggregation: sum\n"
        "    expression: '{[Sales].[Quantity]}'\n"
        "    filterContext:\n"
        "      mode: FIXED\n"
        "  Total Stock On Hand:\n",
        1,
    )
    with pytest.raises(GrainDedupUnsupportedError, match="filter_context"):
        _compile(
            {
                "select": {
                    "dimensions": ["Sale Month"],
                    "measures": ["Unfiltered Quantity", "Total Stock On Hand"],
                }
            },
            yaml_text,
        )


def test_dedup_path_keeps_the_base_measure_data_type_cast() -> None:
    """Taking the column by alias must not drop its declared cast.

    The non-dedup path wraps the component in the measure's ``dataType`` cast.
    Re-aliasing without it silently widened the result: a measure declared
    ``decimal(18, 2)`` came back as a plain integer type once a deduplicated
    measure pulled the query onto the other branch. Asserted on the returned
    column type, because the values compare equal either way.
    """
    con = _cumulative_db()

    reference = _compile(
        {"select": {"dimensions": ["Sale Month"], "measures": ["Running Quantity"]}},
        CUMULATIVE_YAML,
    )
    with_dedup = _compile(
        {
            "select": {
                "dimensions": ["Sale Month"],
                "measures": ["Running Quantity", "Total Stock On Hand"],
            }
        },
        CUMULATIVE_YAML,
    )

    def column_type(sql: str) -> str:
        rel = con.sql(sql)
        return str(dict(zip(rel.columns, rel.types, strict=True))["Running Quantity"])

    assert "DECIMAL" in column_type(reference.sql)
    assert column_type(with_dedup.sql) == column_type(reference.sql)


# --- CFL cannot carry a withinGroup ordering ------------------------------

CFL_WITHIN_GROUP_YAML = """\
version: 1.0
name: cflwg
dataObjects:
  Calendar:
    code: calendar
    schema: main
    columns:
      Date Key: {code: datekey, abstractType: string, primaryKey: true}
      Month: {code: month, abstractType: int}
      Year: {code: year, abstractType: int}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Date Key: {code: datekey, abstractType: string}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: string, primaryKey: true}
      Return Date Key: {code: datekey, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
dimensions:
  Year: {dataObject: Calendar, column: Year, resultType: int}
measures:
  Sales Amount:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Amount]}'
  Return List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns: [{dataObject: Returns, column: Return ID}]
    withinGroup:
      column: {dataObject: Calendar, column: Month}
      order: ASC
"""


CFL_WRAPPED_YAML = (
    CFL_WITHIN_GROUP_YAML
    + """
metrics:
  Wrapped Return List: {dataType: string, expression: '{[Return List]}'}
  Return List Rank:
    type: window
    windowFunction: dense_rank
    measure: Return List
  Wrapped Rank: {dataType: string, expression: '{[Return List Rank]}'}
"""
)


# A sort key that is a *computed* column on an object nothing else in the query
# requires: the leg has to join it purely to project the ordering.
CFL_COMPUTED_ORDER_YAML = CFL_WITHIN_GROUP_YAML.replace(
    """  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: string, primaryKey: true}
      Return Date Key: {code: datekey, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
""",
    """  Reason:
    code: reason
    schema: main
    columns:
      Reason ID: {code: id, abstractType: string, primaryKey: true}
      Severity: {code: severity, abstractType: int}
      Sort Case:
        abstractType: int
        expression: 'CASE WHEN {[Reason].[Severity]} > 2 THEN 1 ELSE 0 END'
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: string, primaryKey: true}
      Return Date Key: {code: datekey, abstractType: string}
      Reason Key: {code: reason_id, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Reason
        columnsFrom: [Reason Key]
        columnsTo: [Reason ID]
""",
).replace(
    """      column: {dataObject: Calendar, column: Month}""",
    """      column: {dataObject: Reason, column: Sort Case}""",
)


# LISTAGG ordered by the very column it aggregates.
CFL_SELF_ORDER_YAML = CFL_WITHIN_GROUP_YAML.replace(
    """      column: {dataObject: Calendar, column: Month}""",
    """      column: {dataObject: Returns, column: Return ID}""",
)


# Same shape, but the LISTAGG declares a non-default delimiter.
CFL_DELIMITER_YAML = CFL_WITHIN_GROUP_YAML.replace(
    """  Return List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns: [{dataObject: Returns, column: Return ID}]
    withinGroup:
      column: {dataObject: Calendar, column: Month}
      order: ASC
""",
    """  Piped Returns:
    resultType: string
    aggregation: listagg
    delimiter: " | "
    columns: [{dataObject: Returns, column: Return ID}]
""",
)


# The sort column sits on Sales, which is not reachable from Returns: both are
# facts hanging off Calendar, so the leg owning the measure cannot project it.
CFL_UNREACHABLE_ORDER_YAML = CFL_WITHIN_GROUP_YAML.replace(
    """    withinGroup:
      column: {dataObject: Calendar, column: Month}
      order: ASC
""",
    """    withinGroup:
      column: {dataObject: Sales, column: Amount}
      order: ASC
""",
).replace("  Return List:", "  Cross Ordered:")


# A model that declares the very name the sort-key column would like to use.
CFL_ALIAS_CLASH_YAML = (
    CFL_WITHIN_GROUP_YAML
    + """  Return List__wg:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Amount]}'
"""
)


def test_a_metric_wrapping_an_ordered_measure_keeps_the_ordering_under_cfl() -> None:
    """A derived metric over an ordered measure is planned like the measure.

    The component is projected into the leg that owns it, sort key included, so
    the wrapper reads an ordered value rather than a rebuilt-unordered one.
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Wrapped Return List"]}},
        CFL_WRAPPED_YAML,
    )
    assert '"Return List__wg"' in result.sql
    assert "ORDER BY" in result.sql


def test_a_derived_metric_over_a_window_metric_is_still_refused_under_cfl() -> None:
    """Unrelated to ordering: that combination has its own guard.

    Kept here because this shape used to be caught by the withinGroup refusal;
    it must keep failing now that the ordering itself is supported.
    """
    with pytest.raises(ResolutionError, match="window metric"):
        _compile(
            {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Wrapped Rank"]}},
            CFL_WRAPPED_YAML,
        )


def test_cfl_carries_an_ordered_aggregates_sort_key_through_the_union() -> None:
    """The leg owning the measure projects the sort key as a column of its own.

    The outer re-aggregation then orders by that column, so the multi-fact plan
    returns the same sequence as the single-fact one instead of an arbitrary
    order (which is what it did before, and why it used to refuse).
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Return List"]}},
        CFL_WITHIN_GROUP_YAML,
    )
    # The sort key rides through the union under its own alias...
    assert '"Calendar"."month" AS "Return List__wg"' in result.sql
    # ...and the outer aggregate orders by it, not by the value column.
    assert '"composite_01"."Return List__wg"' in result.sql


def test_cfl_keeps_the_declared_listagg_delimiter() -> None:
    """The outer rebuild dropped ``separator``, silently falling back to ",".

    A measure declaring ``delimiter: " | "`` came back comma-separated as soon
    as another fact's measure joined the query.
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Piped Returns"]}},
        CFL_DELIMITER_YAML,
    )
    assert "' | '" in result.sql


def test_cfl_projects_a_listagg_source_column_with_its_own_type() -> None:
    """LISTAGG's declared result type resolved to the numeric default.

    Treating it as a numeric aggregate cast the *source* column to that type,
    emitting ``CAST("Returns"."id" AS FLOAT)`` over a text column - SQL every
    engine rejects at execution. Any LISTAGG in a multi-fact query was broken.
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Return List"]}},
        CFL_WITHIN_GROUP_YAML,
    )
    assert "AS FLOAT" not in result.sql.upper()
    assert 'CAST("Returns"."id" AS VARCHAR) AS "Return List"' in result.sql


def test_cfl_refuses_an_ordering_its_own_leg_cannot_reach() -> None:
    """The residual case: the sort column sits on an unreachable object.

    The leg owning the measure has nothing to project, so the aggregate would
    come back arbitrarily ordered. That still refuses rather than reorders.
    """
    from orionbelt.compiler.cfl import WithinGroupNotSupportedInCFLError

    with pytest.raises(WithinGroupNotSupportedInCFLError, match="withinGroup"):
        _compile(
            {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Cross Ordered"]}},
            CFL_UNREACHABLE_ORDER_YAML,
        )


def test_cfl_joins_the_object_behind_a_computed_sort_key() -> None:
    """The sort key can be a computed column, whose object still needs joining.

    ``collect_table_refs`` walked only a handful of node types, so an expression
    expanding to ``CASE`` contributed no tables: the leg projected
    ``CASE WHEN "Reason"."severity" > 2 ...`` over a FROM that never joined
    ``Reason``, and the query failed at execution with
    ``Referenced table "Reason" not found``.
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Return List"]}},
        CFL_COMPUTED_ORDER_YAML,
    )
    assert '"Reason"."severity"' in result.sql
    assert '"main"."reason" AS "Reason"' in result.sql


@pytest.mark.parametrize("dialect", ["duckdb", "clickhouse", "databricks"])
def test_cfl_self_ordering_listagg_compiles_on_array_sorting_dialects(dialect: str) -> None:
    """A self-ordering LISTAGG orders by the measure's own column, not a sort key.

    ClickHouse (``arraySort``) and Databricks (``sort_array``) can only order by
    the aggregated column and compare the two renderings textually, so pointing
    the outer ORDER BY at a separate ``__wg`` alias made them reject an aggregate
    their single-fact path supports.
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Return List"]}},
        CFL_SELF_ORDER_YAML,
        dialect,
    )
    # No redundant sort-key column: the value column is the sort key.
    assert "__wg" not in result.sql


def test_cfl_sort_key_alias_steps_aside_for_a_measure_that_owns_the_name() -> None:
    """``<measure>__wg`` is a legal measure name, so the sort key cannot assume it.

    Declaring ``Return List__wg`` alongside the ordered ``Return List`` used to
    put both in the same composite column - the user's ``SUM`` input in one leg,
    the sort key in the other - so the outer aggregate summed the sort key too
    and the ordering read whatever the sales leg happened to contribute.
    """
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Return List__wg", "Return List"]}},
        CFL_ALIAS_CLASH_YAML,
    )
    # The declared measure keeps the name it asked for...
    assert '"Sales"."amount" AS DECIMAL(18, 2)) AS "Return List__wg"' in result.sql
    # ...and the sort key moves out of its way, in the leg and in the outer ORDER BY.
    assert '"Calendar"."month" AS "Return List__wg_"' in result.sql
    assert 'ORDER BY "composite_01"."Return List__wg_"' in result.sql


def test_cross_column_listagg_order_raises_a_domain_error_not_a_value_error() -> None:
    """Dialects that cannot express it must surface a 422-shaped domain error.

    A bare ``ValueError`` out of codegen would surface as a 500.
    """
    from orionbelt.dialect.base import CrossColumnOrderNotSupportedError

    with pytest.raises(CrossColumnOrderNotSupportedError) as excinfo:
        _compile(
            {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Return List"]}},
            CFL_WITHIN_GROUP_YAML,
            "clickhouse",
        )
    assert excinfo.value.dialect == "clickhouse"
    assert excinfo.value.aggregation == "listagg"


def test_cfl_ordered_aggregate_matches_the_single_fact_result() -> None:
    """Execute both plans: the sequence must be identical.

    This is the assertion the refusal existed to avoid getting wrong - right
    values, wrong sequence - so it is checked against a real engine.
    """
    con = duckdb.connect()
    con.execute("CREATE TABLE calendar (datekey VARCHAR, month INTEGER, year INTEGER)")
    con.execute("INSERT INTO calendar VALUES ('d1', 3, 2024), ('d2', 1, 2024), ('d3', 2, 2024)")
    con.execute("CREATE TABLE sales (id VARCHAR, datekey VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO sales VALUES ('s1','d1',10.0)")
    con.execute("CREATE TABLE returns (id VARCHAR, datekey VARCHAR)")
    # Insertion order deliberately disagrees with month order, so an unordered
    # rebuild returns r1,r2,r3 while the declared ASC ordering is r2,r3,r1.
    con.execute("INSERT INTO returns VALUES ('r1','d1'), ('r2','d2'), ('r3','d3')")

    star = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Return List"]}},
        CFL_WITHIN_GROUP_YAML,
    )
    cfl = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Sales Amount", "Return List"]}},
        CFL_WITHIN_GROUP_YAML,
    )
    star_rows = con.execute(star.sql).fetchall()
    cfl_rows = con.execute(cfl.sql).fetchall()

    assert star_rows == [(2024, "r2,r3,r1")]
    # Same sequence out of the multi-fact plan, alongside the other fact's measure.
    assert [(row[0], row[2]) for row in cfl_rows] == star_rows


def test_the_same_measure_keeps_its_ordering_on_the_single_fact_path() -> None:
    """Only the multi-fact plan is affected; the star path orders correctly."""
    result = _compile(
        {"select": {"dimensions": ["Year"], "measures": ["Return List"]}},
        CFL_WITHIN_GROUP_YAML,
    )
    assert "ORDER BY" in result.sql
    assert '"Calendar"."month"' in result.sql


# --- withinGroup objects must be joined -----------------------------------

WITHIN_GROUP_YAML = """\
version: 1.0
name: wg
dataObjects:
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: string, primaryKey: true}
      Stock On Hand: {code: stock_on_hand, abstractType: int}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Region: {code: region, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
measures:
  Sale List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns: [{dataObject: Sales, column: Sale ID}]
    withinGroup:
      column: {dataObject: Products, column: Stock On Hand}
      order: ASC
"""


def test_within_group_object_is_joined() -> None:
    """``withinGroup`` becomes the aggregate's ORDER BY, so its object must resolve.

    It was never added to the query's required objects, so the compiler emitted
    ``ORDER BY "Products"."stock_on_hand"`` over a FROM containing only
    ``sales`` - valid-looking SQL that every engine rejects at execution with
    ``Referenced table "Products" not found``.
    """
    con = duckdb.connect()
    con.execute("CREATE TABLE products (id VARCHAR, stock_on_hand INTEGER)")
    con.execute("INSERT INTO products VALUES ('p1', 100), ('p2', 7)")
    con.execute("CREATE TABLE sales (id VARCHAR, product_id VARCHAR, region VARCHAR)")
    con.execute("INSERT INTO sales VALUES ('s1','p1','north'), ('s2','p2','north')")

    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sale List"]}}, WITHIN_GROUP_YAML
    )
    assert '"main"."products" AS "Products"' in result.sql

    # p2 (stock 7) sorts before p1 (stock 100).
    assert con.execute(result.sql).fetchall() == [("north", "s2,s1")]


def test_within_group_object_does_not_become_a_fact_table() -> None:
    """It is a join requirement, not a source: it must not enter CFL detection."""
    result = _compile(
        {"select": {"dimensions": ["Region"], "measures": ["Sale List"]}}, WITHIN_GROUP_YAML
    )
    assert result.resolved.fact_tables == ["Sales"]
    assert result.explain is not None
    assert result.explain.planner == "Star Schema"
