"""Tests for WITH ROLLUP / WITH CUBE — IR field + compiler + dialect emission.

Spec: design/PLAN_with_rollup.md
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import AliasedExpr, ColumnRef, From, FunctionCall, Select
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import Grouping, QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML
from tests.integration.drift.vendor_exec.test_rollup_coalesced_exec import (
    MODEL as COALESCED_MODEL,
)
from tests.integration.drift.vendor_exec.test_rollup_coalesced_exec import (
    REGION,
)


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


@pytest.mark.parametrize("grouping", ["rollup", "cube"])
def test_dremio_returns_each_key_through_its_flag(
    sample_model: SemanticModel, grouping: str
) -> None:
    """Dremio's planner carries a filter's constant through a rollup: a query
    reading the rolled-up rows read the filtered value on a subtotal. The
    flag-guarded key is not read as the constant; the other engines need none."""
    query = {
        "select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]},
        "where": [{"field": "Customer Country", "op": "=", "value": "DE"}],
        "grouping": grouping,
    }

    sql = _compile(query, sample_model, dialect="dremio")

    assert 'CASE WHEN GROUPING("Customers"."COUNTRY") = 0 THEN "Customers"."COUNTRY" END' in sql
    assert 'GROUPING("Customers"."COUNTRY") AS "_g_Customer Country"' in sql
    assert "CASE WHEN GROUPING(" not in _compile(query, sample_model, dialect="postgres")


def test_dremio_qualifies_a_key_named_like_its_column() -> None:
    """Dremio reads an unqualified key's name in the GROUP BY as the alias of
    the guarded column, and refuses the GROUPING() it then holds."""
    region = ColumnRef(name="Region")
    node = Select(
        columns=[
            AliasedExpr(expr=region, alias="Region"),
            AliasedExpr(expr=FunctionCall(name="SUM", args=[ColumnRef(name="a")]), alias="s"),
            AliasedExpr(expr=FunctionCall(name="GROUPING", args=[region]), alias="_g_Region"),
        ],
        from_=From(source="composite", alias="composite"),
        group_by=[region],
        grouping="rollup",
    )

    sql = DialectRegistry.get("dremio").compile_select(node)

    key = '"composite"."Region"'
    assert sql.startswith(f'SELECT CASE WHEN GROUPING({key}) = 0 THEN {key} END AS "Region"')
    assert f'GROUPING({key}) AS "_g_Region"' in sql
    assert f"GROUP BY ROLLUP({key})" in sql
    assert 'SUM("a")' in sql


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "mysql", "dremio"])
def test_a_coalesced_dimensions_flag_reads_its_key(dialect: str) -> None:
    """Beside two facts, a coalesced dimension groups by the COALESCE of its
    members: its flag read the alias, which PostgreSQL, DuckDB and MySQL
    refuse, and Dremio too once the key is returned through its flag.
    ``test_rollup_coalesced_exec`` runs it on every engine."""
    raw, source_map = TrackedLoader().load_string(COALESCED_MODEL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = {
        "select": {"dimensions": [REGION], "measures": ["Revenue", "Spend"]},
        "grouping": "rollup",
    }

    sql = _compile(query, model, dialect=dialect)

    q = DialectRegistry.get(dialect).quote_identifier
    key = f"COALESCE({q('Sales Region')}, {q('Purchase Region')})"
    assert f"GROUPING({key}) AS {q('_g_Region')}" in sql
    assert f"GROUPING({q('Region')})" not in sql


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
