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

from orionbelt.models.query import QueryObject, QuerySelect
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
metrics:
  Stock Share:
    expression: '{{[Total Stock]}} / {{[Grand Stock]}}'
  Share of Region Stock:
    expression: '{{[Total Stock]}} / {{[Region Stock]}}'
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
                measures=["Stock Share", "Share of Region Stock"],
            )
        ),
    )
    shares = {
        (r["region"], r["category"]): (
            _ratio(r["stock share"]),
            _ratio(r["share of region stock"]),
        )
        for r in rows
    }
    expected = {
        ("north", "tools"): (210 / 510, 1.0),
        ("south", "parts"): (300 / 510, 300 / 400),
        ("south", "tools"): (100 / 510, 100 / 400),
    }
    assert shares == {
        k: (round(Decimal(str(a)), 4), round(Decimal(str(b)), 4)) for k, (a, b) in expected.items()
    }, f"{target.name}: {shares}"


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
