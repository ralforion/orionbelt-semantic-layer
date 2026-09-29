"""Tests for SQL validation using sqlglot."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot.errors import SqlglotError

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.validator import (
    _DIALECT_MAP,
    _FORMAT_MEMO,
    _FormatMemo,
    format_sql,
    validate_sql,
)
from orionbelt.dialect import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


def _load_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Model errors: {[e.message for e in result.errors]}"
    return model


@pytest.mark.parametrize(
    "dialect",
    ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "mysql", "postgres", "snowflake"],
)
def test_valid_sql_all_dialects(dialect: str) -> None:
    errors = validate_sql("SELECT 1", dialect)
    assert errors == []


def test_invalid_sql_returns_errors() -> None:
    errors = validate_sql("SELECT FROM WHERE", "postgres")
    assert len(errors) > 0


def test_dremio_maps_to_trino() -> None:
    errors = validate_sql("SELECT 1 AS x", "dremio")
    assert errors == []


def test_dialect_map_covers_all_registered_dialects() -> None:
    """Every registered dialect must have an entry in the validator's _DIALECT_MAP."""
    registered = set(DialectRegistry.available())
    mapped = set(_DIALECT_MAP.keys())
    missing = registered - mapped
    assert not missing, (
        f"Dialects registered but missing from validator _DIALECT_MAP: {sorted(missing)}. "
        f"Add them to _DIALECT_MAP in compiler/validator.py."
    )


def test_unknown_dialect_returns_warning() -> None:
    errors = validate_sql("SELECT 1", "unknown_db")
    assert len(errors) == 1
    assert "Unknown dialect" in errors[0]


def test_validation_integrated_in_pipeline() -> None:
    model = _load_model()
    pipeline = CompilationPipeline()
    query = QueryObject(
        select=QuerySelect(
            dimensions=["Customer Country"],
            measures=["Total Revenue"],
        ),
    )
    result = pipeline.compile(query, model, "postgres")
    assert result.sql_valid is True
    assert result.sql != ""


def test_pipeline_returns_sql_even_when_invalid() -> None:
    model = _load_model()
    pipeline = CompilationPipeline()
    query = QueryObject(
        select=QuerySelect(
            dimensions=["Customer Country"],
            measures=["Total Revenue"],
        ),
    )
    result = pipeline.compile(query, model, "postgres")
    # SQL is always returned regardless of validation
    assert result.sql != ""
    assert isinstance(result.sql_valid, bool)


_PARITY_SQL = [
    "SELECT a, SUM(b) AS s FROM t GROUP BY a ORDER BY s DESC LIMIT 5",
    "SELECT CAST(DATE_TRUNC('month', d) AS DATE) FROM t WHERE x IN (1, 2)",
    "WITH c AS (SELECT 1 AS x) SELECT x FROM c UNION ALL SELECT 2",
    "SELCT a FROM t",
    "SELECT a FROM",
    "SELECT (a FROM t",
    "SELECT 'unterminated FROM t",
]


def _transpile_errors(sql: str, sg_dialect: str) -> list[str]:
    """What validation reported when it used ``transpile`` (parse + generate)."""
    try:
        sqlglot.transpile(sql, read=sg_dialect)
    except SqlglotError as exc:
        return [str(exc)]
    return []


@pytest.mark.parametrize("dialect", sorted(_DIALECT_MAP))
@pytest.mark.parametrize("sql", _PARITY_SQL)
def test_parse_only_validation_reports_what_transpile_did(dialect: str, sql: str) -> None:
    assert validate_sql(sql, dialect) == _transpile_errors(sql, _DIALECT_MAP[dialect])


@pytest.mark.parametrize("dialect", sorted(_DIALECT_MAP))
@pytest.mark.parametrize("sql", _PARITY_SQL)
def test_memoized_format_matches_transpile(dialect: str, sql: str) -> None:
    sg = _DIALECT_MAP[dialect]
    try:
        expected = sqlglot.transpile(sql, read=sg, write=sg, pretty=True)[0]
    except SqlglotError:
        expected = sql
    assert format_sql(sql, dialect) == expected
    assert format_sql(sql, dialect) == expected  # the memoized answer too


def test_repeated_format_is_served_from_the_memo() -> None:
    _FORMAT_MEMO.clear()
    sql = "SELECT a, b FROM t WHERE a > 1"
    first = format_sql(sql, "postgres")
    assert len(_FORMAT_MEMO) == 1
    assert format_sql(sql, "postgres") is first


def test_format_unknown_dialect_returns_input_uncached() -> None:
    _FORMAT_MEMO.clear()
    assert format_sql("SELECT 1", "nope") == "SELECT 1"
    assert len(_FORMAT_MEMO) == 0


def test_oversized_sql_is_formatted_but_not_kept() -> None:
    """A very large accepted query must not pin memory (review, #494)."""
    _FORMAT_MEMO.clear()
    columns = ", ".join(f"c{i}" for i in range(_FORMAT_MEMO.max_entry_chars // 4))
    sql = f"SELECT {columns} FROM t"
    assert len(sql) > _FORMAT_MEMO.max_entry_chars
    formatted = format_sql(sql, "postgres")
    assert formatted.startswith("SELECT")
    assert len(_FORMAT_MEMO) == 0
    assert _FORMAT_MEMO.chars == 0


def test_memo_is_bounded_by_characters_least_recently_used_first() -> None:
    memo = _FormatMemo(max_entries=100, max_chars=30, max_entry_chars=100)
    memo.put("aaaa", "d", "AAAA")  # 8 chars
    memo.put("bbbb", "d", "BBBB")  # 16
    memo.put("cccc", "d", "CCCC")  # 24
    assert memo.get("aaaa", "d") == "AAAA"  # refresh a
    memo.put("dddd", "d", "DDDD")  # 32 > 30: evicts b, the least recently used
    assert memo.get("bbbb", "d") is None
    assert memo.get("aaaa", "d") == "AAAA"
    assert memo.chars <= 30


def test_memo_is_bounded_by_entries() -> None:
    memo = _FormatMemo(max_entries=2, max_chars=10_000, max_entry_chars=100)
    for sql in ("s1", "s2", "s3"):
        memo.put(sql, "d", sql.upper())
    assert len(memo) == 2
    assert memo.get("s1", "d") is None
    assert memo.chars == len("s2S2s3S3")


def test_replacing_an_entry_keeps_the_character_count_exact() -> None:
    memo = _FormatMemo(max_entries=10, max_chars=10_000, max_entry_chars=100)
    memo.put("sql", "d", "X")
    memo.put("sql", "d", "XYZ")
    assert (len(memo), memo.chars) == (1, len("sqlXYZ"))
