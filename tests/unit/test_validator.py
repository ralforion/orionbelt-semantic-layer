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
from orionbelt.dialect.clickhouse import STATEMENT_SETTINGS
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


#: How the Dremio dialect writes text outside Latin-1, which Trino's parser
#: rejected with "Expecting )" although Dremio runs it.
_DREMIO_UTF8 = (
    "SELECT SUBSTR(_UTF8'it''s €', 1) AS \"r\", '_UTF8 ''y' AS \"s\" "
    'FROM "t" WHERE "t"."v" IN (SUBSTR(_UTF8\'東京\', 1), \'café\')'
)


def test_dremio_utf8_literals_validate() -> None:
    assert validate_sql(_DREMIO_UTF8, "dremio") == []


def test_dremio_utf8_literals_format_as_dremio_reads_them() -> None:
    """sqlglot writes ``_UTF8 '...'``, which Dremio rejects; the space goes,
    and a literal holding the same characters keeps them.
    """
    formatted = format_sql(_DREMIO_UTF8, "dremio")
    assert "SUBSTR(_UTF8'it''s €', 1)" in formatted
    assert "SUBSTR(_UTF8'東京', 1)" in formatted
    assert "'_UTF8 ''y'" in formatted
    assert "_UTF8 '" not in formatted.replace("'_UTF8 ''y'", "")


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


@pytest.mark.parametrize(
    "body",
    [
        "SELECT a, SUM(b) AS s FROM t GROUP BY ALL",
        "SELECT a, SUM(b) AS s FROM t GROUP BY a WITH ROLLUP",
    ],
    ids=["group-by-all", "with-rollup"],
)
def test_the_clickhouse_settings_suffix_validates_and_formats(body: str) -> None:
    """sqlglot rejects ``SETTINGS`` after these, which ClickHouse accepts: the
    statement is checked without it, and formatted with it kept."""
    sql = f"{body}\n{STATEMENT_SETTINGS}"
    assert validate_sql(sql, "clickhouse") == []
    assert format_sql(sql, "clickhouse").endswith(f"\n{STATEMENT_SETTINGS}")


def test_a_clickhouse_statement_with_a_left_join_reads_null_for_an_unmatched_row() -> None:
    query = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
    )
    result = CompilationPipeline().compile(query, _load_model(), "clickhouse")
    assert "LEFT JOIN" in result.sql
    assert result.sql.endswith(f"\n{STATEMENT_SETTINGS}")
    assert result.sql_valid


def test_a_clickhouse_statement_without_a_join_sets_nothing() -> None:
    """A ``readonly = 1`` user may not change a setting, and without an outer
    join the setting changes nothing: such a statement goes without it."""
    query = QueryObject(select=QuerySelect(measures=["Total Revenue"]))
    result = CompilationPipeline().compile(query, _load_model(), "clickhouse")
    assert "JOIN" not in result.sql
    assert "SETTINGS" not in result.sql


_JOIN_WORDS_MODEL = """
version: "1.0"
name: join_words
dataObjects:
  Orders:
    code: orders
    columns:
      Order Id: {code: id, abstractType: string, primaryKey: true}
      Label: {code: label, abstractType: string}
      Amount: {code: amount, abstractType: float}
dimensions:
  Label: {dataObject: Orders, column: Label, resultType: string}
measures:
  Left Join Amount:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""


@pytest.mark.parametrize(
    "label",
    ["LEFT JOIN", "it's a FULL OUTER JOIN", "back\\slash' RIGHT JOIN"],
    ids=["literal", "quote-in-literal", "backslash-in-literal"],
)
def test_join_words_in_a_literal_or_an_alias_set_nothing(label: str) -> None:
    """Only an outer join gets the setting: the words in a string or a quoted
    name are no join, and a ``readonly = 1`` user would be refused for them."""
    raw, source_map = TrackedLoader().load_string(_JOIN_WORDS_MODEL)
    model, validation = ReferenceResolver().resolve(raw, source_map)
    assert validation.valid, validation.errors
    query = QueryObject(
        select=QuerySelect(dimensions=["Label"], measures=["Left Join Amount"]),
        where=[{"field": "Label", "op": "equals", "value": label}],
    )
    result = CompilationPipeline().compile(query, model, "clickhouse")
    assert "JOIN" in result.sql
    assert "SETTINGS" not in result.sql


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
