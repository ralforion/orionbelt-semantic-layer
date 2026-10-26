"""Dimension-value search on every engine.

The search's queries (``service/value_search.py``) are ordinary dimension-only
queries: a listing, an ``ilike`` candidate query with an escaped pattern, the
fuzzy stage's domain query, and a date range. Each is compiled for the engine
and run on it here.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.value_search import QueryRun, ValueSearchConfig, search_values

from .conftest import VendorTarget
from .test_reaggregate_exec import SCHEMAS, _create

pytestmark = pytest.mark.docker

CUSTOMERS = "vs_customers"

ROWS = [
    ("c1", "Germany", "2024-03-01"),
    ("c2", "France", "2023-11-02"),
    ("c3", "Georgia", "2025-01-15"),
    ("c4", "East Germany", "2022-06-30"),
    ("c5", "50%_off", "2024-08-08"),
    ("c6", "Testland", "2025-02-01"),
]

MODEL_YAML = """
version: "1.0"
name: value_search_vendor
dataObjects:
  Customers:
    code: {customers}
    schema: '{schema}'
    columns:
      Cust ID: {{code: id, abstractType: string, primaryKey: true}}
      Country: {{code: country, abstractType: string}}
      Signed Up: {{code: signed_up, abstractType: date}}
dimensions:
  Country: {{dataObject: Customers, column: Country, resultType: string}}
  Signed Up: {{dataObject: Customers, column: Signed Up, resultType: date}}
filters:
  - {{dataObject: Customers, column: Country, operator: notequals, value: Testland}}
"""


def _prepare(target: VendorTarget) -> SemanticModel:
    # The shared string type is CHAR(8) on MySQL, too short for "East Germany".
    country: int | str = "CHAR(20)" if target.dialect == "mysql" else 0
    _create(target, CUSTOMERS, [("id", 0), ("country", country), ("signed_up", 3)], ROWS)
    yaml_text = MODEL_YAML.format(customers=CUSTOMERS, schema=SCHEMAS.get(target.dialect, ""))
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _search(target: VendorTarget, model: SemanticModel, dimension: str, **kwargs: object):
    async def run(query: QueryObject) -> QueryRun:
        compiled = CompilationPipeline().compile(query, model, target.dialect)
        rows = target.execute(compiled.sql)
        return QueryRun(values=[next(iter(row.values())) for row in rows], sql=compiled.sql)

    return asyncio.run(
        search_values(model, dimension, run=run, config=ValueSearchConfig(), **kwargs)
    )


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)

    listing = _search(target, model, "Country")
    assert sorted(m.value for m in listing.values) == [
        "50%_off",
        "East Germany",
        "France",
        "Georgia",
        "Germany",
    ]

    found = _search(target, model, "Country", search="GERM")
    assert [(m.value, m.match) for m in found.values] == [
        ("Germany", "prefix"),
        ("East Germany", "contains"),
    ]

    # The wildcards in the term are literal on every engine.
    literal = _search(target, model, "Country", search="%_")
    assert [m.value for m in literal.values] == ["50%_off"]

    fuzzy = _search(target, model, "Country", search="Germny", limit=1)
    assert [(m.value, m.match) for m in fuzzy.values] == [("Germany", "fuzzy")]

    span = _search(target, model, "Signed Up")
    # Testland's customer is filtered out by the model.
    assert (str(span.minimum)[:10], str(span.maximum)[:10]) == ("2022-06-30", "2025-01-15")
    assert date.fromisoformat(str(span.minimum)[:10]) == date(2022, 6, 30)


def test_duckdb_value_search(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_value_search(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_value_search(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_value_search(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_value_search(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_value_search(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_value_search(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_value_search(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
