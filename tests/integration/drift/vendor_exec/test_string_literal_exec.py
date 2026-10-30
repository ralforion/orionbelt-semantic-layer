"""A string literal survives the round trip, on every engine.

``compile_expr`` doubled the quote and left the backslash alone, which is the
SQL standard and right on two engines out of seven. Measured with that
rendering:

===========  ===================================  ==========================
engine       ``a\\b``                              ``it's``
===========  ===================================  ==========================
DuckDB       ok                                   ok
PostgreSQL   ok                                   ok
MySQL        ``a\\x08`` - a backspace, silently    ok
ClickHouse   ``a\\x08``, and ``C:\\temp\\x`` raises  ok
Snowflake    ``a\\x08``, and ``C:\\temp\\x`` raises  ok
BigQuery     ``a\\x08``, and ``C:\\temp\\x`` raises  **raises** - reads it as
                                                  two concatenated literals
Databricks   ``a\\x08``                             **``its``**, silently
===========  ===================================  ==========================

So a Windows path, a regex or an escaped delimiter in a filter was silently
wrong on five engines, and an apostrophe - the one case the old code was
written for - was wrong on two, one of them silently.

Both conventions are *wrong* on the other side rather than merely unnecessary,
which is why this is a dialect property and not a wider escape.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import Literal
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

#: Values a model could legitimately filter on. The apostrophe is here because
#: the old rendering handled it and still got it wrong on two engines.
VALUES = [
    "plain",
    "it's",
    'quote"double',
    "a\\b",
    "C:\\temp\\x",
    "back\\\\slash",
    "tab\\there",
    "percent%_underscore",
    "unicode: é ü 中",
    # Real control characters, not the escaped spellings. The first round of
    # this test used "tab\\there" - a literal backslash and a t - so a genuine
    # newline was never exercised, and BigQuery rejects one inside a quoted
    # string with "Unclosed string literal".
    "line\nbreak",
    "carriage\rreturn",
    "crlf\r\nboth",
    "real\ttab",
    "form\x0cfeed",
    "control\x01byte",
    # Outside Latin-1, which Dremio types a plain literal as: it failed to
    # plan each of these as a value of its own.
    "İstanbul",
    "€ 東京 😀",
    "it's İ",
    "a\\b €",
]

#: A dimension computed from text outside Latin-1, filtered on and grouped by.
MODEL = """\
version: 1.0
dataObjects:
  Places:
    code: places
    columns:
      City: {code: city, abstractType: string}
      Amount: {code: amount, abstractType: int}
      Region:
        expression: >-
          CASE WHEN {City} = 'İstanbul' THEN 'Türkiye €' ELSE coalesce({City}, '東京') END
        abstractType: string
dimensions:
  City: {dataObject: Places, column: City, resultType: string}
  Region: {dataObject: Places, column: Region, resultType: string}
measures:
  Total: {columns: [{dataObject: Places, column: Amount}], aggregation: sum, resultType: int}
  Cities:
    columns: [{dataObject: Places, column: City}]
    aggregation: listagg
    delimiter: " · "
    resultType: string
"""

#: (city, amount): the table the model reads.
ROWS = [("İstanbul", 1), ("café", 2), ("東京", 4), (None, 8), ("x", 16)]

#: (filter, {region: total}): each filter compares text outside Latin-1, with a
#: column or with the computed dimension.
FILTERS = [
    (
        {"field": "City", "op": "in", "value": ["İstanbul", "東京", "café"]},
        {"Türkiye €": 1, "café": 2, "東京": 4},
    ),
    ({"field": "Region", "op": "equals", "value": "東京"}, {"東京": 12}),
    ({"field": "Region", "op": "like", "value": "Tür%"}, {"Türkiye €": 1}),
    ({"field": "City", "op": "notequals", "value": "İstanbul"}, {"café": 2, "東京": 4, "x": 16}),
]


def _assert_round_trip(target: VendorTarget) -> None:
    dialect = DialectRegistry.get(target.dialect)
    mismatches = []
    for raw in VALUES:
        literal = dialect.compile_expr(Literal.string(raw))
        try:
            got = next(iter(target.execute(f"SELECT {literal} AS v")[0].values()))
        except Exception as exc:  # noqa: BLE001 - a raise is a failure like any other
            mismatches.append(f"{raw!r} -> raised {str(exc)[:60]}")
            continue
        if got != raw:
            mismatches.append(f"{raw!r} -> {got!r} (emitted {literal})")
    assert not mismatches, f"{target.name} mangles string literals:\n  " + "\n  ".join(mismatches)


#: How each engine spells a string type in ``CAST(NULL AS ...)``.
TEXT = {
    "bigquery": "STRING",
    "clickhouse": "Nullable(String)",
    "databricks": "STRING",
    "mysql": "CHAR",
}


def _assert_model(target: VendorTarget) -> None:
    """The model's queries, compiled for the engine, over :data:`ROWS`."""
    dialect = DialectRegistry.get(target.dialect)
    q = dialect.quote_identifier
    null = f"CAST(NULL AS {TEXT.get(target.dialect, 'VARCHAR')})"
    rows = " UNION ALL ".join(
        f"SELECT {null if city is None else dialect.quote_string_literal(city)} AS {q('city')}, "
        f"{amount} AS {q('amount')}"
        for city, amount in ROWS
    )
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    mismatches = []
    for where, expected in FILTERS:
        select = {"dimensions": ["Region"], "measures": ["Total", "Cities"]}
        query = QueryObject.model_validate({"select": select, "where": [where]})
        sql = store.compile_query(model_id, query, target.dialect).sql
        try:
            result = target.execute(f"WITH {q('places')} AS ({rows})\n{sql}")
        except Exception as exc:  # noqa: BLE001 - a raise is a failure like any other
            mismatches.append(f"{where} -> raised {str(exc)[:80]}")
            continue
        got = {}
        for raw in result:
            row = {key.lower(): value for key, value in raw.items()}
            got[row["region"]] = int(row["total"])
            city = "İstanbul" if row["region"] == "Türkiye €" else row["region"]
            if row["cities"] != city:
                mismatches.append(f"{where} -> cities {row['cities']!r}, expected {city!r}")
        if got != expected:
            mismatches.append(f"{where} -> {got}, expected {expected}")
    assert not mismatches, f"{target.name} text outside Latin-1:\n  " + "\n  ".join(mismatches)


def test_duckdb_string_literals(vendor_duckdb: VendorTarget) -> None:
    _assert_round_trip(vendor_duckdb)


def test_postgres_string_literals(vendor_postgres: VendorTarget) -> None:
    _assert_round_trip(vendor_postgres)


def test_mysql_string_literals(vendor_mysql: VendorTarget) -> None:
    _assert_round_trip(vendor_mysql)


def test_clickhouse_string_literals(vendor_clickhouse: VendorTarget) -> None:
    _assert_round_trip(vendor_clickhouse)


def test_bigquery_string_literals(vendor_bigquery: VendorTarget) -> None:
    _assert_round_trip(vendor_bigquery)


def test_snowflake_string_literals(vendor_snowflake: VendorTarget) -> None:
    _assert_round_trip(vendor_snowflake)


def test_databricks_string_literals(vendor_databricks: VendorTarget) -> None:
    _assert_round_trip(vendor_databricks)


def test_dremio_string_literals(vendor_dremio: VendorTarget) -> None:
    _assert_round_trip(vendor_dremio)


def test_duckdb_text_outside_latin1_in_a_model(vendor_duckdb: VendorTarget) -> None:
    _assert_model(vendor_duckdb)


def test_postgres_text_outside_latin1_in_a_model(vendor_postgres: VendorTarget) -> None:
    _assert_model(vendor_postgres)


def test_mysql_text_outside_latin1_in_a_model(vendor_mysql: VendorTarget) -> None:
    _assert_model(vendor_mysql)


def test_clickhouse_text_outside_latin1_in_a_model(vendor_clickhouse: VendorTarget) -> None:
    _assert_model(vendor_clickhouse)


def test_bigquery_text_outside_latin1_in_a_model(vendor_bigquery: VendorTarget) -> None:
    _assert_model(vendor_bigquery)


def test_snowflake_text_outside_latin1_in_a_model(vendor_snowflake: VendorTarget) -> None:
    _assert_model(vendor_snowflake)


def test_databricks_text_outside_latin1_in_a_model(vendor_databricks: VendorTarget) -> None:
    _assert_model(vendor_databricks)


def test_dremio_text_outside_latin1_in_a_model(vendor_dremio: VendorTarget) -> None:
    _assert_model(vendor_dremio)
