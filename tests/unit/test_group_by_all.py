"""Tests for ``GROUP BY ALL`` emission on modern OLAP dialects.

Snowflake (2022+), Databricks/Spark (3.4+), DuckDB (0.7+), BigQuery, and
ClickHouse (22.6+) accept ``GROUP BY ALL`` as a shorter, functionally
equivalent form of an explicit grouping list — the engine derives the
list from the SELECT clause. The dialect capability flag
``supports_group_by_all`` controls whether the planner emits it.

Postgres, MySQL, and Dremio still emit the explicit list.

See issue #91.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML

_SUPPORTING_DIALECTS = (
    "snowflake",
    "databricks",
    "duckdb",
    "bigquery",
    "clickhouse",
)
_NON_SUPPORTING_DIALECTS = ("postgres", "mysql", "dremio")


@pytest.fixture
def sample_model() -> SemanticModel:
    loader = TrackedLoader()
    raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
    resolver = ReferenceResolver()
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


def _compile(query_dict: dict, model: SemanticModel, dialect: str) -> str:
    query = QueryObject.model_validate(query_dict)
    return CompilationPipeline().compile(query, model, dialect).sql


@pytest.mark.parametrize("dialect", _SUPPORTING_DIALECTS)
def test_supporting_dialects_advertise_capability(dialect: str) -> None:
    """The capability flag must be set on every dialect that emits
    ``GROUP BY ALL``, so the ``/v1/dialects`` listing matches actual
    emission behaviour.
    """
    assert DialectRegistry.get(dialect).capabilities.supports_group_by_all is True


@pytest.mark.parametrize("dialect", _NON_SUPPORTING_DIALECTS)
def test_non_supporting_dialects_do_not_advertise(dialect: str) -> None:
    assert DialectRegistry.get(dialect).capabilities.supports_group_by_all is False


@pytest.mark.parametrize("dialect", _SUPPORTING_DIALECTS)
def test_plain_group_by_emits_all(dialect: str, sample_model: SemanticModel) -> None:
    """No grouping modifier (default) on a supporting dialect emits
    ``GROUP BY ALL`` instead of the explicit column list.
    """
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            }
        },
        sample_model,
        dialect,
    )
    assert "GROUP BY ALL" in sql, f"{dialect}: expected GROUP BY ALL, got:\n{sql}"


@pytest.mark.parametrize("dialect", _NON_SUPPORTING_DIALECTS)
def test_plain_group_by_emits_explicit_list_on_non_supporting(
    dialect: str, sample_model: SemanticModel
) -> None:
    """Postgres / MySQL / Dremio keep the explicit grouping list. The
    important guard is that ``GROUP BY ALL`` never leaks to a dialect
    that would reject it as a syntax error.
    """
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            }
        },
        sample_model,
        dialect,
    )
    assert "GROUP BY ALL" not in sql, f"{dialect}: must not emit GROUP BY ALL"
    assert "GROUP BY" in sql, f"{dialect}: expected explicit GROUP BY, got:\n{sql}"


@pytest.mark.parametrize("dialect", _SUPPORTING_DIALECTS)
def test_rollup_modifier_keeps_explicit_list(dialect: str, sample_model: SemanticModel) -> None:
    """``GROUP BY ALL`` is incompatible with ROLLUP / CUBE — the
    modifier needs the explicit column list inside ``ROLLUP(...)`` /
    ``CUBE(...)``. Even on supporting dialects the planner must fall
    back to the explicit form when a grouping modifier is present.
    """
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "rollup",
        },
        sample_model,
        dialect,
    )
    assert "GROUP BY ALL" not in sql, (
        f"{dialect}: GROUP BY ALL leaked into a ROLLUP query — would be a "
        f"syntax error on the engine. SQL:\n{sql}"
    )
    # ClickHouse: trailing ``WITH ROLLUP``; others: ``GROUP BY ROLLUP(...)``.
    assert "ROLLUP" in sql, f"{dialect}: ROLLUP missing from:\n{sql}"


@pytest.mark.parametrize("dialect", _SUPPORTING_DIALECTS)
def test_cube_modifier_keeps_explicit_list(dialect: str, sample_model: SemanticModel) -> None:
    sql = _compile(
        {
            "select": {
                "dimensions": ["Customer Country"],
                "measures": ["Total Revenue"],
            },
            "grouping": "cube",
        },
        sample_model,
        dialect,
    )
    assert "GROUP BY ALL" not in sql, f"{dialect}: GROUP BY ALL leaked into CUBE query"
    assert "CUBE" in sql, f"{dialect}: CUBE missing from:\n{sql}"


def test_no_group_by_when_only_measures(sample_model: SemanticModel) -> None:
    """A query with no dimensions has no GROUP BY at all — neither the
    explicit nor the ALL form should appear. Regression guard so the
    capability flag does not slip ``GROUP BY ALL`` into measure-only
    aggregate queries (the engine would still accept it, but it is
    misleading SQL).
    """
    sql = _compile(
        {"select": {"dimensions": [], "measures": ["Total Revenue"]}},
        sample_model,
        "duckdb",
    )
    assert "GROUP BY" not in sql
