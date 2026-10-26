"""Dimension-value search, compiled and run on DuckDB.

Every step is an ordinary dimension-only query (``service/value_search.py``),
so the model's static filter (no ``Testland``) restricts the values as it
restricts any query.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.value_search import (
    QueryRun,
    ValueSearchConfig,
    ValueSearchError,
    ValueSearchResult,
    search_values,
)

MODEL_YAML = """
version: "1.0"
name: value_search
dataObjects:
  Customers:
    code: customers
    columns:
      Cust Id: {code: id, abstractType: string, primaryKey: true}
      Country: {code: country, abstractType: string}
      Tier: {code: tier, abstractType: string}
      Email: {code: email, abstractType: string}
      Signed Up: {code: signed_up, abstractType: date}
dimensions:
  Country: {dataObject: Customers, column: Country, resultType: string}
  Tier: {dataObject: Customers, column: Tier, resultType: string}
  Email: {dataObject: Customers, column: Email, resultType: string, valueSearch: false}
  Signed Up: {dataObject: Customers, column: Signed Up, resultType: date}
filters:
  - {dataObject: Customers, column: Country, operator: notequals, value: Testland}
"""

ROWS = [
    ("c1", "Germany", "gold", "a@x.de", date(2024, 3, 1)),
    ("c2", "Germany", "silver", "b@x.de", date(2024, 5, 9)),
    ("c3", "France", "gold", "c@x.fr", date(2023, 11, 2)),
    ("c4", "Georgia", "silver", "d@x.ge", date(2025, 1, 15)),
    ("c5", "East Germany", "gold", "e@x.de", date(2022, 6, 30)),
    ("c6", "Testland", "gold", "f@x.tt", date(2025, 2, 1)),
    ("c7", "50%_off", "silver", "g@x.zz", date(2024, 8, 8)),
]


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customers (id VARCHAR, country VARCHAR, tier VARCHAR,"
        " email VARCHAR, signed_up DATE)"
    )
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", ROWS)
    return con


async def _search(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    dimension: str,
    config: ValueSearchConfig | None = None,
    **kwargs: Any,
) -> ValueSearchResult:
    async def run(query: QueryObject) -> QueryRun:
        compiled = CompilationPipeline().compile(query, model, "duckdb")
        rows = con.execute(compiled.sql).fetchall()
        return QueryRun(values=[row[0] for row in rows], sql=compiled.sql)

    return await search_values(
        model, dimension, run=run, config=config or ValueSearchConfig(), **kwargs
    )


def _pairs(result: ValueSearchResult) -> list[tuple[Any, str]]:
    return [(m.value, m.match) for m in result.values]


async def test_listing_is_ordered_and_obeys_the_static_filter(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    result = await _search(model, con, "Country")
    assert [m.value for m in result.values] == [
        "50%_off",
        "East Germany",
        "France",
        "Georgia",
        "Germany",
    ]
    assert {m.match for m in result.values} == {"listed"}
    assert not result.truncated
    assert "Testland" in result.sql[0]


async def test_listing_is_truncated_at_the_limit(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    result = await _search(model, con, "Country", limit=2)
    assert [m.value for m in result.values] == ["50%_off", "East Germany"]
    assert result.truncated


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("germany", [("Germany", "exact"), ("East Germany", "contains")]),
        ("ge", [("Georgia", "prefix"), ("Germany", "prefix")]),
        ("many", [("East Germany", "contains"), ("Germany", "contains")]),
        ("%_", [("50%_off", "contains")]),
    ],
    ids=["exact-first", "prefix", "contains", "wildcards-are-literal"],
)
async def test_search_ranks_exact_prefix_contains(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    term: str,
    expected: list[tuple[str, str]],
) -> None:
    result = await _search(model, con, "Country", search=term, limit=2)
    assert _pairs(result) == expected


async def test_a_misspelling_finds_the_value_by_fuzzy_match(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    result = await _search(model, con, "Country", search="Germny", limit=1)
    assert _pairs(result) == [("Germany", "fuzzy")]
    assert result.values[0].score is not None
    # The candidate query, then the domain for the fuzzy stage.
    assert len(result.sql) == 2


async def test_no_fuzzy_stage_over_the_domain_cap(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    config = ValueSearchConfig(fuzzy_domain_max=3)
    result = await _search(model, con, "Country", config=config, search="Germny")
    assert result.values == []
    assert any("no fuzzy matching" in w for w in result.warnings)


async def test_where_narrows_the_values(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    gold = [{"field": "Tier", "op": "=", "value": "gold"}]
    result = await _search(model, con, "Country", where=gold)
    assert [m.value for m in result.values] == ["East Germany", "France", "Germany"]


async def test_a_date_dimension_returns_its_range(
    model: SemanticModel, con: duckdb.DuckDBPyConnection
) -> None:
    result = await _search(model, con, "Signed Up")
    # Testland's customer (2025-02-01) is filtered out by the model.
    assert (result.minimum, result.maximum) == (date(2022, 6, 30), date(2025, 1, 15))
    assert result.values == []


@pytest.mark.parametrize(
    ("dimension", "kwargs", "code", "status"),
    [
        ("Email", {}, "VALUE_SEARCH_DISABLED", 403),
        ("Signed Up", {"search": "2024"}, "VALUE_SEARCH_NOT_SUPPORTED", 400),
        ("Nope", {}, "UNKNOWN_DIMENSION", 400),
        ("Country", {"limit": 0}, "INVALID_LIMIT", 400),
        ("Country", {"limit": 201}, "INVALID_LIMIT", 400),
    ],
    ids=["opt-out", "range-search", "unknown", "limit-zero", "limit-over-cap"],
)
async def test_refusals(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    dimension: str,
    kwargs: dict[str, Any],
    code: str,
    status: int,
) -> None:
    with pytest.raises(ValueSearchError) as excinfo:
        await _search(model, con, dimension, **kwargs)
    assert (excinfo.value.code, excinfo.value.status) == (code, status)


def test_the_opt_out_reaches_the_graph_and_an_inheriting_model(model: SemanticModel) -> None:
    from rdflib import Literal

    from orionbelt.obsl.exporter import OBSL, export_obsl
    from orionbelt.service.model_store import ModelStore

    graph = export_obsl(model, "value_search")
    opted_out = list(graph.subject_objects(OBSL.valueSearch))
    # Only the opt-out is stated, on the one dimension declaring it.
    assert len(opted_out) == 1 and opted_out[0][1] == Literal(False)

    raw = ModelStore._model_to_raw(model)
    assert raw["dimensions"]["Email"]["valueSearch"] is False  # type: ignore[index]
    assert "valueSearch" not in raw["dimensions"]["Country"]  # type: ignore[index]
