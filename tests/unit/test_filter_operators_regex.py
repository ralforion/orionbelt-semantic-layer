"""Tests for the regex / blank / length filter operators."""

from __future__ import annotations

import pytest

import orionbelt.dialect  # noqa: F401 — registers all 8 dialects
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


def _model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, sm = loader.load_string(SAMPLE_MODEL_YAML)
    model, _ = resolver.resolve(raw, sm)
    return model


def _compile(op: FilterOperator, value: object, dialect: str = "postgres") -> str:
    """Helper: compile a one-filter query and return the WHERE-fragment."""
    query = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"]),
        where=[QueryFilter(field="Customer Country", op=op, value=value)],
    )
    return CompilationPipeline().compile(query, _model(), dialect_name=dialect).sql


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------


class TestRegex:
    def test_postgres_uses_tilde(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]{2}$", dialect="postgres")
        assert '"Customers"."COUNTRY" ~ ' in sql
        assert "'^[A-Z]{2}$'" in sql

    def test_postgres_negated_uses_bang_tilde(self) -> None:
        sql = _compile(FilterOperator.NOT_REGEX, "^X", dialect="postgres")
        assert '"Customers"."COUNTRY" !~ ' in sql

    def test_mysql_uses_regexp(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="mysql")
        assert "REGEXP" in sql

    def test_clickhouse_uses_match_function(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="clickhouse")
        assert "match(" in sql

    def test_databricks_uses_rlike(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="databricks")
        assert "RLIKE" in sql

    def test_bigquery_uses_regexp_contains(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="bigquery")
        assert "REGEXP_CONTAINS(" in sql

    def test_duckdb_uses_regexp_matches(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="duckdb")
        assert "regexp_matches(" in sql

    def test_snowflake_uses_regexp_like(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="snowflake")
        assert "REGEXP_LIKE(" in sql

    def test_dremio_uses_regexp_like(self) -> None:
        sql = _compile(FilterOperator.REGEX, "^[A-Z]+$", dialect="dremio")
        assert "REGEXP_LIKE(" in sql

    def test_non_string_pattern_rejected(self) -> None:
        from orionbelt.compiler.resolution import ResolutionError

        with pytest.raises(ResolutionError) as excinfo:
            _compile(FilterOperator.REGEX, 42)
        assert any(e.code == "INVALID_FILTER_VALUE" for e in excinfo.value.errors)


# ---------------------------------------------------------------------------
# Blank / not blank
# ---------------------------------------------------------------------------


class TestBlank:
    def test_blank_emits_null_or_trim_empty(self) -> None:
        sql = _compile(FilterOperator.BLANK, None)
        assert '"Customers"."COUNTRY" IS NULL' in sql
        assert "TRIM(" in sql
        assert "= ''" in sql

    def test_not_blank_emits_not_null_and_trim_nonempty(self) -> None:
        sql = _compile(FilterOperator.NOT_BLANK, None)
        assert '"Customers"."COUNTRY" IS NOT NULL' in sql
        assert "TRIM(" in sql
        assert "<> ''" in sql

    def test_blank_works_on_all_dialects(self) -> None:
        # Smoke: every dialect produces something that compiles.
        for d in (
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ):
            sql = _compile(FilterOperator.BLANK, None, dialect=d)
            assert "TRIM" in sql.upper()


# ---------------------------------------------------------------------------
# Length
# ---------------------------------------------------------------------------


class TestLength:
    def test_length_eq_emits_length_equals(self) -> None:
        sql = _compile(FilterOperator.LENGTH_EQ, 2)
        assert "LENGTH(" in sql
        assert "= 2" in sql

    def test_length_gt(self) -> None:
        sql = _compile(FilterOperator.LENGTH_GT, 5)
        assert "LENGTH(" in sql
        assert "> 5" in sql

    def test_length_lt(self) -> None:
        sql = _compile(FilterOperator.LENGTH_LT, 10)
        assert "LENGTH(" in sql
        assert "< 10" in sql

    def test_non_int_value_rejected(self) -> None:
        from orionbelt.compiler.resolution import ResolutionError

        with pytest.raises(ResolutionError) as excinfo:
            _compile(FilterOperator.LENGTH_EQ, "two")
        assert any(e.code == "INVALID_FILTER_VALUE" for e in excinfo.value.errors)

    def test_bool_rejected_even_though_isinstance_int(self) -> None:
        # ``True`` is an int subclass — guard rejects it explicitly.
        from orionbelt.compiler.resolution import ResolutionError

        with pytest.raises(ResolutionError) as excinfo:
            _compile(FilterOperator.LENGTH_EQ, True)
        assert any(e.code == "INVALID_FILTER_VALUE" for e in excinfo.value.errors)
