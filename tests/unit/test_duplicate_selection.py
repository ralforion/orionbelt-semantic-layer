"""A dimension, measure or metric selected twice is refused.

Each one named the same output column twice (``SELECT tier AS "Tier", tier AS
"Tier"``), and a client reading the result by name saw only one of them. A
dimension at two grains is two columns (``Order Date``, ``Order Date:year``)
and stays allowed.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel


def _compile(sales_model: SemanticModel, dimensions: list[str], measures: list[str]) -> str:
    query = QueryObject.model_validate({"select": {"dimensions": dimensions, "measures": measures}})
    return CompilationPipeline().compile(query, sales_model, "duckdb").sql


@pytest.mark.parametrize(
    ("dimensions", "message"),
    [
        (
            ["Customer Country", "Customer Country"],
            "'Customer Country' selects the same column as 'Customer Country'",
        ),
        # ``Order Date`` declares ``timeGrain: month``.
        (
            ["Order Date", "Order Date:month"],
            "'Order Date:month' selects the same column as 'Order Date:month'",
        ),
    ],
    ids=["named twice", "the declared grain named"],
)
def test_a_dimension_twice(sales_model: SemanticModel, dimensions: list[str], message: str) -> None:
    with pytest.raises(ResolutionError) as exc:
        _compile(sales_model, dimensions, ["Revenue"])
    errors = exc.value.errors
    assert [e.code for e in errors] == ["DUPLICATE_DIMENSION"]
    assert errors[0].message.startswith(message)
    assert errors[0].path == "select.dimensions"


@pytest.mark.parametrize("name", ["Revenue", "Revenue per Order"], ids=["measure", "metric"])
def test_a_measure_or_metric_twice(sales_model: SemanticModel, name: str) -> None:
    with pytest.raises(ResolutionError) as exc:
        _compile(sales_model, ["Customer Country"], [name, "Order Count", name])
    errors = exc.value.errors
    assert [e.code for e in errors] == ["DUPLICATE_MEASURE"]
    assert errors[0].message == f"'{name}' is selected twice; list each measure once"
    assert errors[0].path == "select.measures"


def test_a_dimension_at_two_grains_is_two_columns(sales_model: SemanticModel) -> None:
    sql = _compile(sales_model, ["Order Date", "Order Date:year"], ["Revenue"])
    assert 'AS "Order Date"' in sql and 'AS "Order Date:year"' in sql
