"""A deduplicated measure at a grain of its own, executed on every engine.

The products are replicated by the sales joined to them: p1 is sold in both
regions. A measure on the products is deduplicated at the query's grain, and
one with ``total: true`` or a ``grain`` override at that grain instead, in a
CTE of its own: each product once per region (north 210, south 400), or once
overall (510), never the per-region values summed (610).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create, _number, _run

pytestmark = pytest.mark.docker

PRODUCTS = "dedup_products"
SALES = "dedup_sales"

PRODUCT_ROWS = [("p1", 100, "tools"), ("p2", 110, "tools"), ("p3", 300, "parts")]
SALE_ROWS = [
    ("s1", "p1", "north", 1),
    ("s2", "p2", "north", 1),
    ("s3", "p1", "south", 1),
    ("s4", "p3", "south", 1),
]

MODEL_YAML = """
version: "1.0"
name: grain_dedup_vendor
dataObjects:
  Products:
    code: {products}
    schema: '{schema}'
    columns:
      Product ID: {{code: id, abstractType: string, primaryKey: true}}
      Stock: {{code: stock, abstractType: float}}
      Category: {{code: category, abstractType: string}}
  Sales:
    code: {sales}
    schema: '{schema}'
    columns:
      Sale ID: {{code: id, abstractType: string, primaryKey: true}}
      Product: {{code: product_id, abstractType: string}}
      Region: {{code: region, abstractType: string}}
      Quantity: {{code: quantity, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product ID]}}
dimensions:
  Region: {{dataObject: Sales, column: Region, resultType: string}}
  Category: {{dataObject: Products, column: Category, resultType: string}}
  Units Sold: {{dataObject: Sales, column: Quantity}}
measures:
  Sold Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
  Total Stock:
    columns: [{{dataObject: Products, column: Stock}}]
    aggregation: sum
  Grand Stock:
    columns: [{{dataObject: Products, column: Stock}}]
    aggregation: sum
    total: true
  Region Stock:
    columns: [{{dataObject: Products, column: Stock}}]
    aggregation: sum
    grain: {{mode: FIXED, keepOnly: [Region]}}
  Filtered Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
    filterContext: {{mode: FIXED}}
  Grand Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
    total: true
  Region Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
    grain: {{mode: FIXED, keepOnly: [Region]}}
metrics:
  Region Stock Rank:
    type: window
    windowFunction: dense_rank
    measure: Region Stock
    orderDirection: desc
  Region Stock per Quantity:
    expression: '{{[Region Stock]}} / {{[Filtered Quantity]}}'
  Stock Share:
    expression: '{{[Total Stock]}} / {{[Grand Stock]}}'
  Share of Region Stock:
    expression: '{{[Total Stock]}} / {{[Region Stock]}}'
  Stock per Grand Quantity:
    expression: '{{[Total Stock]}} / {{[Grand Quantity]}}'
  Stock per Region Quantity:
    expression: '{{[Total Stock]}} / {{[Region Quantity]}}'
  Doubled Grand Quantity:
    expression: '{{[Grand Quantity]}} * 2'
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    # A float stock: a ratio of integer sums divides as integers on Dremio,
    # which is no matter of deduplication.
    _create(target, PRODUCTS, [("id", 0), ("stock", 1), ("category", 0)], PRODUCT_ROWS)
    _create(
        target,
        SALES,
        [("id", 0), ("product_id", 0), ("region", 0), ("quantity", 2)],
        SALE_ROWS,
    )
    yaml_text = MODEL_YAML.format(
        products=PRODUCTS, sales=SALES, schema=SCHEMAS.get(target.dialect, "")
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _ratio(value: object) -> Decimal | None:
    number = _number(value)
    return None if number is None else round(number, 4)


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)

    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Category"],
                measures=["Sold Quantity", "Total Stock", "Region Stock", "Grand Stock"],
            )
        ),
    )
    got = {
        (r["region"], r["category"]): tuple(
            _number(r[m]) for m in ("sold quantity", "total stock", "region stock", "grand stock")
        )
        for r in rows
    }
    assert got == {
        ("north", "tools"): (Decimal(2), Decimal(210), Decimal(210), Decimal(510)),
        ("south", "parts"): (Decimal(1), Decimal(300), Decimal(400), Decimal(510)),
        ("south", "tools"): (Decimal(1), Decimal(100), Decimal(400), Decimal(510)),
    }, f"{target.name}: {got}"

    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Category"],
                measures=["Stock Share", "Share of Region Stock", "Region Stock per Quantity"],
            )
        ),
    )
    shares = {
        (r["region"], r["category"]): (
            _ratio(r["stock share"]),
            _ratio(r["share of region stock"]),
            _ratio(r["region stock per quantity"]),
        )
        for r in rows
    }
    # The last beside a filterContext component, which splits the metric again
    # and reads the deduplicated one by name: quantity is 2, 1, 1.
    expected = {
        ("north", "tools"): (210 / 510, 1.0, 105),
        ("south", "parts"): (300 / 510, 300 / 400, 400),
        ("south", "tools"): (100 / 510, 100 / 400, 400),
    }
    assert shares == {
        k: tuple(round(Decimal(str(v)), 4) for v in values) for k, values in expected.items()
    }, f"{target.name}: {shares}"

    _assert_having_after_filter_context(target, model)
    _assert_fact_side_components(target, model)
    _assert_rollup(target, model)
    _assert_period_over_period(target)
    _assert_multi_fact(target)


def _assert_rollup(target: VendorTarget, model: SemanticModel) -> None:
    """Each subtotal counts a product once at its own grain: p1, sold in both
    regions, makes the grand total 510, not the regions' 210 + 400."""
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Category"], measures=["Sold Quantity", "Total Stock"]
            ),
            grouping="rollup",
        ),
    )
    got = {
        (r["region"], r["category"]): (_number(r["sold quantity"]), _number(r["total stock"]))
        for r in rows
    }
    expected = {
        ("north", "tools"): (2, 210),
        ("south", "parts"): (1, 300),
        ("south", "tools"): (1, 100),
        ("north", None): (2, 210),
        ("south", None): (2, 400),
        (None, None): (4, 510),
    }
    assert got == {k: (Decimal(q), Decimal(v)) for k, (q, v) in expected.items()}, (
        f"{target.name}: {got}"
    )

    # A numeric dimension declaring no resultType, which defaults to string:
    # the subtotal rows' NULL must take the column's own type, not that one.
    # ClickHouse shows a non-nullable column's default (0) on a subtotal row
    # rather than NULL, so the rolled-up value is read by its flag.
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(dimensions=["Region", "Units Sold"], measures=["Total Stock"]),
            grouping="rollup",
        ),
    )
    by_units = {
        (
            r["region"],
            None if r["units sold"] is None or r["_g_units sold"] else int(r["units sold"]),
        ): _number(r["total stock"])
        for r in rows
    }
    assert by_units == {
        ("north", 1): Decimal(210),
        ("south", 1): Decimal(400),
        ("north", None): Decimal(210),
        ("south", None): Decimal(400),
        (None, None): Decimal(510),
    }, f"{target.name}: {by_units}"


def _assert_fact_side_components(target: VendorTarget, model: SemanticModel) -> None:
    """A split metric's fact-side component with a total (4 units) or a grain
    override (2 per region), computed by the dedup pass at that grain, and a
    metric that reads the same total without needing a split (8)."""
    metrics = ["Stock per Grand Quantity", "Stock per Region Quantity", "Doubled Grand Quantity"]
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Region", "Category"], measures=metrics)),
    )
    got = {(r["region"], r["category"]): tuple(_ratio(r[m.lower()]) for m in metrics) for r in rows}
    expected = {
        ("north", "tools"): (52.5, 105, 8),
        ("south", "parts"): (75, 150, 8),
        ("south", "tools"): (25, 50, 8),
    }
    assert got == {
        k: tuple(round(Decimal(str(v)), 4) for v in values) for k, values in expected.items()
    }, f"{target.name}: {got}"


def _assert_having_after_filter_context(target: VendorTarget, model: SemanticModel) -> None:
    """Filtered to tools, the south's quantity is 1 under the query's filter
    and 2 under the filterContext: its stock per quantity is 100 / 2 = 50, so
    ``< 60`` keeps it. Applied before the filterContext value existed, the
    predicate read 100 / 1 and dropped it."""
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(dimensions=["Region"], measures=["Region Stock per Quantity"]),
            where=[QueryFilter(field="Category", op="=", value="tools")],
            having=[QueryFilter(field="Region Stock per Quantity", op="<", value=60)],
        ),
    )
    got = {r["region"]: _ratio(r["region stock per quantity"]) for r in rows}
    assert got == {"south": Decimal(50)}, f"{target.name}: {got}"

    # A separate predicate on a deduplicated measure is dedup's to apply; the
    # filterContext pass does not rebuild it as a HAVING over dedup's output.
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Category"],
                measures=["Region Stock per Quantity", "Region Stock"],
            ),
            having=[
                QueryFilter(field="Region Stock per Quantity", op=">", value=0),
                QueryFilter(field="Region Stock", op=">", value=300),
            ],
        ),
    )
    kept = sorted((r["region"], r["category"]) for r in rows)
    assert kept == [("south", "parts"), ("south", "tools")], f"{target.name}: {kept}"

    # A rank over the deduplicated measure is filtered after the ranking, not
    # by comparing the stock itself with 1.
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Region", "Category"], measures=["Region Stock", "Region Stock Rank"]
            ),
            having=[QueryFilter(field="Region Stock Rank", op="<=", value=1)],
        ),
    )
    ranked = sorted((r["region"], r["category"]) for r in rows)
    assert ranked == [("south", "parts"), ("south", "tools")], f"{target.name}: {ranked}"


POP_PRODUCTS = "dedup_pop_products"
POP_SALES = "dedup_pop_sales"

POP_MODEL_YAML = """
version: "1.0"
name: grain_dedup_pop_vendor
dataObjects:
  Products:
    code: {products}
    schema: '{schema}'
    columns:
      Product ID: {{code: id, abstractType: string, primaryKey: true}}
      Stock: {{code: stock, abstractType: float}}
  Sales:
    code: {sales}
    schema: '{schema}'
    columns:
      Sale ID: {{code: id, abstractType: string, primaryKey: true}}
      Product: {{code: product_id, abstractType: string}}
      Sale Date: {{code: sale_date, abstractType: date}}
      Quantity: {{code: quantity, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product ID]}}
dimensions:
  Sale Month: {{dataObject: Sales, column: Sale Date, resultType: date, timeGrain: month}}
measures:
  Sold Quantity:
    columns: [{{dataObject: Sales, column: Quantity}}]
    aggregation: sum
  Total Stock:
    columns: [{{dataObject: Products, column: Stock}}]
    aggregation: sum
metrics:
  Stock Growth:
    type: period_over_period
    expression: '{{[Total Stock]}}'
    periodOverPeriod:
      {{timeDimension: Sale Month, grain: month, offset: -1, offsetGrain: month,
        comparison: difference}}
  Stock per Quantity:
    expression: '{{[Total Stock]}} / {{[Sold Quantity]}}'
  Stock Rank:
    type: window
    windowFunction: dense_rank
    measure: Total Stock
    orderDirection: desc
  Doubled Stock Rank:
    expression: '{{[Stock Rank]}} * 2'
  Count Growth:
    type: period_over_period
    expression: '{{[Products Count]}}'
    periodOverPeriod:
      {{timeDimension: Sale Month, grain: month, offset: -1, offsetGrain: month,
        comparison: difference}}
"""


def _assert_period_over_period(target: VendorTarget) -> None:
    """p1 sells twice in January: its stock counts once per month (100, not
    200), February's p2 is 110, so the growth is 10 and the stock per unit
    100 / 3 and 110 / 4. One product a month (a ``COUNT(1)``), so no count
    growth. March's sale is of a product that does not exist: no product, so
    no stock and a count of 0."""
    _create(target, POP_PRODUCTS, [("id", 0), ("stock", 1)], [("p1", 100), ("p2", 110)])
    _create(
        target,
        POP_SALES,
        [("id", 0), ("product_id", 0), ("sale_date", 3), ("quantity", 2)],
        [
            ("s1", "p1", "2026-01-05", 1),
            ("s2", "p1", "2026-01-20", 2),
            ("s3", "p2", "2026-02-10", 4),
            ("s4", "p9", "2026-03-03", 1),
        ],
    )
    yaml_text = POP_MODEL_YAML.format(
        products=POP_PRODUCTS, sales=POP_SALES, schema=SCHEMAS.get(target.dialect, "")
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    metrics = ["Total Stock", "Stock Growth", "Stock per Quantity", "Count Growth"]
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Sale Month"], measures=metrics)),
    )
    got = {str(r["sale month"])[:7]: tuple(_ratio(r[m.lower()]) for m in metrics) for r in rows}
    expected = {
        "2026-01": (100, None, 100 / 3, None),
        "2026-02": (110, 10, 110 / 4, 0),
        "2026-03": (None, None, None, -1),
    }
    assert got == {
        k: tuple(None if v is None else round(Decimal(str(v)), 4) for v in values)
        for k, values in expected.items()
    }, f"{target.name}: {got}"

    # A derived metric over a window metric whose base measure is the
    # deduplicated one: January's stock is 100, so it ranks below February's
    # 110 (inflated to 200 it ranked first). March, with no stock, is left out:
    # engines place NULL at either end of the ranking.
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Sale Month"], measures=["Stock Rank", "Doubled Stock Rank"]
            ),
            where=[QueryFilter(field="Sale Month", op="<", value="2026-03-01")],
        ),
    )
    # With the window metric beside it, both read one base measure column,
    # which PostgreSQL called ambiguous when it was projected twice.
    ranks = {
        str(r["sale month"])[:7]: (int(r["stock rank"]), int(r["doubled stock rank"])) for r in rows
    }
    assert ranks == {"2026-01": (2, 4), "2026-02": (1, 2)}, f"{target.name}: {ranks}"


CFL_DATES = "dedup_cfl_dates"
CFL_PRODUCTS = "dedup_cfl_products"
CFL_SALES = "dedup_cfl_sales"
CFL_REFUNDS = "dedup_cfl_refunds"

CFL_MODEL_YAML = """
version: "1.0"
name: grain_dedup_cfl_vendor
dataObjects:
  Dates:
    code: {dates}
    schema: '{schema}'
    columns:
      Date Key: {{code: id, abstractType: string, primaryKey: true}}
      Month: {{code: month, abstractType: int}}
  Products:
    code: {products}
    schema: '{schema}'
    columns:
      Product ID: {{code: id, abstractType: string, primaryKey: true}}
      Stock: {{code: stock, abstractType: float}}
  Sales:
    code: {sales}
    schema: '{schema}'
    columns:
      Sale ID: {{code: id, abstractType: string, primaryKey: true}}
      Date: {{code: date_id, abstractType: string}}
      Product: {{code: product_id, abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Dates, columnsFrom: [Date], columnsTo: [Date Key]}}
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [Product ID]}}
  Refunds:
    code: {refunds}
    schema: '{schema}'
    columns:
      Refund ID: {{code: id, abstractType: string, primaryKey: true}}
      Date: {{code: date_id, abstractType: string}}
      Refund: {{code: refund, abstractType: float}}
    joins:
      - {{joinType: many-to-one, joinTo: Dates, columnsFrom: [Date], columnsTo: [Date Key]}}
dimensions:
  Month: {{dataObject: Dates, column: Month, resultType: int}}
measures:
  Total Stock:
    columns: [{{dataObject: Products, column: Stock}}]
    aggregation: sum
  Refund Amount:
    columns: [{{dataObject: Refunds, column: Refund}}]
    aggregation: sum
"""


def _assert_multi_fact(target: VendorTarget) -> None:
    """Sales and refunds share only the dates, so the query is a UNION ALL,
    and the sales leg replicates products: p1 sold twice in month 1 counts
    once (100, one product), and the sale of a missing product p9 in month 2
    is no product at all."""
    _create(target, CFL_DATES, [("id", 0), ("month", 2)], [("d1", 1), ("d2", 2)])
    _create(target, CFL_PRODUCTS, [("id", 0), ("stock", 1)], [("p1", 100), ("p2", 110)])
    _create(
        target,
        CFL_SALES,
        [("id", 0), ("date_id", 0), ("product_id", 0)],
        [("s1", "d1", "p1"), ("s2", "d1", "p1"), ("s3", "d2", "p2"), ("s4", "d2", "p9")],
    )
    _create(
        target,
        CFL_REFUNDS,
        [("id", 0), ("date_id", 0), ("refund", 1)],
        [("r1", "d1", 1), ("r2", "d2", 4)],
    )
    yaml_text = CFL_MODEL_YAML.format(
        dates=CFL_DATES,
        products=CFL_PRODUCTS,
        sales=CFL_SALES,
        refunds=CFL_REFUNDS,
        schema=SCHEMAS.get(target.dialect, ""),
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    metrics = ["Total Stock", "Products Count", "Refund Amount"]
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Month"], measures=metrics)),
    )
    got = {int(r["month"]): tuple(_ratio(r[m.lower()]) for m in metrics) for r in rows}
    expected = {1: (100, 1, 1), 2: (110, 1, 4)}
    assert got == {
        k: tuple(round(Decimal(str(v)), 4) for v in values) for k, values in expected.items()
    }, f"{target.name}: {got}"


def test_duckdb_grain_dedup(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_grain_dedup(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_grain_dedup(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_grain_dedup(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_grain_dedup(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_grain_dedup(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_grain_dedup(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_grain_dedup(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
