"""An unknown time grain in a query is a typed error naming the valid grains.

``Order Date:day_of_week`` raised pydantic's untyped ``'day_of_week' is not a
valid TimeGrain`` out of the compiler, which the REST API answered with a 500.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel, TimeGrain


def _errors(sales_model: SemanticModel, query: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(QueryObject.model_validate(query), sales_model, "duckdb")
    return [(e.code, e.message) for e in excinfo.value.errors]


@pytest.mark.parametrize(
    "dimension",
    [
        "Order Date:day_of_week",
        {"coalesce": ["Order Date:day_of_week", "Order Date"], "as": "Day"},
    ],
    ids=["select", "coalesce"],
)
def test_an_unknown_grain_is_invalid_time_grain(
    sales_model: SemanticModel, dimension: object
) -> None:
    errors = _errors(sales_model, {"select": {"dimensions": [dimension], "measures": ["Revenue"]}})
    assert len(errors) == 1, errors
    code, message = errors[0]
    assert code == "INVALID_TIME_GRAIN"
    assert "'day_of_week'" in message and "'Order Date:day_of_week'" in message
    assert all(grain.value in message for grain in TimeGrain)


def test_it_is_reported_beside_the_query_s_other_errors(sales_model: SemanticModel) -> None:
    errors = _errors(
        sales_model,
        {"select": {"dimensions": ["Order Date:fortnight", "Nope"], "measures": ["Revenue"]}},
    )
    assert [code for code, _ in errors] == ["INVALID_TIME_GRAIN", "UNKNOWN_DIMENSION"]


def test_a_valid_grain_still_compiles(sales_model: SemanticModel) -> None:
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]}}
    )
    assert CompilationPipeline().compile(query, sales_model, "duckdb").sql
