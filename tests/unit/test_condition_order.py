"""The result cache keys on the SQL of a query with its AND-ed filters in a fixed order.

Filters given in another order return the same rows; they must share a cache
entry. Anything else that tells two queries apart must still give two keys.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from orionbelt.compiler import condition_order
from orionbelt.compiler.condition_order import ordered_query
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML

DIALECTS = ["postgres", "duckdb", "snowflake", "mysql", "bigquery", "clickhouse", "databricks"]
COMMERCE_MODEL = Path(__file__).parents[2] / "examples" / "orionbelt_1_commerce.yaml"
SELECT = {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}


def _load(text: str) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture
def sample_model() -> SemanticModel:
    return _load(SAMPLE_MODEL_YAML)


def _not(value: str) -> dict[str, Any]:
    return {"field": "Customer Country", "op": "notequals", "value": value}


def _query(**parts: Any) -> QueryObject:
    return QueryObject.model_validate({"select": SELECT, **parts})


def _keys(model: SemanticModel, *queries: QueryObject) -> list[str]:
    return [CompilationPipeline().compile(q, model, "postgres").key_sql for q in queries]


def _assert_shared_key(model: SemanticModel, first: QueryObject, second: QueryObject) -> None:
    one, two = (CompilationPipeline().compile(q, model, "postgres") for q in (first, second))
    assert one.sql != two.sql  # the SQL that runs keeps the order given
    assert one.key_sql == two.key_sql


# ── Which filters move ──────────────────────────────────────────────────────


def test_where_and_having_lists_are_ordered() -> None:
    having = [
        {"field": "Total Revenue", "op": "gt", "value": 10},
        {"field": "Order Count", "op": "gt", "value": 1},
    ]
    first = _query(where=[_not("FR"), _not("DE")], having=having)
    second = _query(where=[_not("DE"), _not("FR")], having=having[::-1])
    assert first != second
    assert ordered_query(first) == ordered_query(second)


def test_the_filters_of_an_and_group_are_ordered_and_an_or_group_keeps_its_order() -> None:
    def group(logic: str, *values: str) -> dict[str, Any]:
        return {"logic": logic, "filters": [_not(v) for v in values]}

    assert ordered_query(_query(where=[group("and", "FR", "DE")])) == ordered_query(
        _query(where=[group("and", "DE", "FR")])
    )
    assert ordered_query(_query(where=[group("or", "FR", "DE")])) != ordered_query(
        _query(where=[group("or", "DE", "FR")])
    )


def test_an_and_group_inside_an_or_group_is_ordered() -> None:
    def query(*values: str) -> QueryObject:
        inner = {"logic": "and", "negated": True, "filters": [_not(v) for v in values]}
        return _query(where=[{"logic": "or", "filters": [inner, _not("IT")]}])

    assert ordered_query(query("FR", "DE")) == ordered_query(query("DE", "FR"))


def test_the_filters_of_an_exists_subquery_are_ordered() -> None:
    def query(*values: str) -> QueryObject:
        inner = [{"field": "Customer Country", "op": "equals", "value": v} for v in values]
        exists = {
            "field": "Customer Country",
            "op": "exists",
            "subquery": {"dataObject": "Orders", "filter": inner},
        }
        return _query(where=[exists])

    assert ordered_query(query("FR", "DE")) == ordered_query(query("DE", "FR"))


def test_a_query_whose_filters_are_in_order_is_returned_equal() -> None:
    query = _query(where=[_not("DE"), _not("FR")])
    assert ordered_query(query) == query


# ── The key ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dialect_name", DIALECTS)
def test_filters_in_another_order_share_a_key(
    sample_model: SemanticModel, dialect_name: str
) -> None:
    first = _query(where=[_not("FR"), _not("DE")])
    second = _query(where=[_not("DE"), _not("FR")])
    pipeline = CompilationPipeline()
    one = pipeline.compile(first, sample_model, dialect_name)
    two = pipeline.compile(second, sample_model, dialect_name)
    assert one.sql != two.sql  # the SQL that runs keeps the order given
    assert one.key_sql == two.key_sql


def test_other_differences_still_give_two_keys(sample_model: SemanticModel) -> None:
    keys = _keys(
        sample_model,
        _query(where=[_not("FR"), _not("DE")]),
        _query(where=[_not("FR"), _not("IT")]),
        _query(where=[_not("FR"), _not("DE")], limit=5),
        _query(
            where=[_not("FR"), _not("DE")],
            orderBy=[{"field": "Total Revenue", "direction": "desc"}],
        ),
        _query(where=[{"logic": "or", "filters": [_not("FR"), _not("DE")]}]),
    )
    assert len(set(keys)) == len(keys)


def test_key_sql_is_the_sql_when_nothing_moves(sample_model: SemanticModel) -> None:
    result = CompilationPipeline().compile(_query(), sample_model, "postgres")
    assert result.cache_key_sql is None
    assert result.key_sql == result.sql


def test_period_over_period_filters_in_another_order_share_a_key() -> None:
    """The PoP CTEs carry the filters as text the compiler writes itself."""
    model = _load(COMMERCE_MODEL.read_text())
    select = {"dimensions": ["Sales Month"], "measures": ["Sales YoY Growth"]}
    where = [
        {"field": "Client Name", "op": "equals", "value": "Kai Garcia"},
        {"field": "Product Name", "op": "equals", "value": "Laptop"},
    ]
    one, two = (
        QueryObject.model_validate({"select": select, "where": w}) for w in (where, where[::-1])
    )
    _assert_shared_key(model, one, two)


def test_deeply_grouped_filters_still_compile(sample_model: SemanticModel) -> None:
    """The reordered query nests its groups as deep as the one written.

    Four AND groups of 128 filters compile; flattened into one chain of 512
    they would exceed the recursion limit of the SQL renderer.
    """

    def query(step: int) -> QueryObject:
        groups = [
            {"logic": "and", "filters": [_not(f"{g}-{i}") for i in range(128)][::step]}
            for g in range(4)
        ]
        return _query(where=groups)

    _assert_shared_key(sample_model, query(1), query(-1))


def test_a_reordered_query_the_compiler_refuses_keeps_its_own_key(
    sample_model: SemanticModel,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key costs the query a shared entry at worst, never its result."""
    refused = _query(where=[{"field": "No Such Field", "op": "equals", "value": 1}])
    monkeypatch.setattr(condition_order, "ordered_query", lambda _query: refused)
    with caplog.at_level(logging.WARNING, logger="orionbelt.compiler.pipeline"):
        result = CompilationPipeline().compile(
            _query(where=[_not("FR"), _not("DE")]), sample_model, "postgres"
        )
    assert result.cache_key_sql is None
    assert result.key_sql == result.sql
    assert "reordered failed" in caplog.text
