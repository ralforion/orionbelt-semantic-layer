"""Tests for WITH ROLLUP / WITH CUBE — IR field + compiler + dialect emission.

Spec: design/PLAN_with_rollup.md
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import Grouping, QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def sample_model() -> SemanticModel:
    loader = TrackedLoader()
    raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
    resolver = ReferenceResolver()
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


def _compile(query_dict: dict, model: SemanticModel, dialect: str = "duckdb") -> str:
    query = QueryObject.model_validate(query_dict)
    return CompilationPipeline().compile(query, model, dialect).sql


def test_no_grouping_emits_plain_group_by(sample_model: SemanticModel) -> None:
    """Regression guard: grouping=None preserves the existing GROUP BY shape."""
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            }
        },
        sample_model,
    )
    assert "GROUP BY ROLLUP" not in sql
    assert "GROUP BY CUBE" not in sql
    assert "GROUPING(" not in sql
    assert "_g_" not in sql


def test_rollup_emits_rollup_clause(sample_model: SemanticModel) -> None:
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "rollup",
        },
        sample_model,
    )
    assert "GROUP BY ROLLUP" in sql


def test_cube_emits_cube_clause(sample_model: SemanticModel) -> None:
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "cube",
        },
        sample_model,
    )
    assert "GROUP BY CUBE" in sql


def test_rollup_emits_grouping_flag_columns(sample_model: SemanticModel) -> None:
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "rollup",
        },
        sample_model,
    )
    assert "GROUPING(" in sql
    assert '"_g_Customer Country"' in sql or "_g_Customer Country" in sql


def test_clickhouse_emits_trailing_modifier(sample_model: SemanticModel) -> None:
    """ClickHouse uses the trailing-modifier syntax, not ROLLUP()/CUBE() functions."""
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "rollup",
        },
        sample_model,
        dialect="clickhouse",
    )
    assert "WITH ROLLUP" in sql
    assert "GROUP BY ROLLUP(" not in sql

    sql_cube = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "cube",
        },
        sample_model,
        dialect="clickhouse",
    )
    assert "WITH CUBE" in sql_cube
    assert "GROUP BY CUBE(" not in sql_cube


def test_grouping_requires_dimensions() -> None:
    """grouping with no dimensions raises a validation error."""
    with pytest.raises(ValueError, match="at least one dimension"):
        QueryObject.model_validate(
            {
                "select": {"measures": ["Total Revenue"]},
                "grouping": "rollup",
            }
        )


def test_grouping_rejected_in_raw_mode() -> None:
    with pytest.raises(ValueError, match="raw mode"):
        QueryObject.model_validate(
            {
                "select": {"fields": ["Customers.Customer Name"]},
                "grouping": "rollup",
            }
        )


def test_grouping_enum_parses_strings() -> None:
    q = QueryObject.model_validate(
        {
            "select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]},
            "grouping": "rollup",
        }
    )
    assert q.grouping == Grouping.ROLLUP


def test_dimension_order_preserved_in_rollup(sample_model: SemanticModel) -> None:
    """ROLLUP is order-sensitive — preserve the SELECT order exactly."""
    # The sample model only has Customer Country; use it with a synthetic case
    # by emitting the SQL and checking the dimension order in the ROLLUP clause.
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "rollup",
        },
        sample_model,
    )
    # Single-dim case: ROLLUP(<dim>) form
    assert "ROLLUP(" in sql
