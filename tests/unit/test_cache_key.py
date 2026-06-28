"""Tests for cache.key — deterministic key construction.

v2 (2026-05): keys hash on compiled SQL strings, not QueryObject dicts.
Two callers that compile to the same SQL get the same key, regardless
of how they assembled it (OBSQL, QueryObject, OBML YAML).

v3 (2026-06): keys are scoped to the data source, not the session. Any
session resolving to the same datasource/model/dialect/SQL shares the entry.
"""

from __future__ import annotations

import pytest

from orionbelt.cache.key import build_cache_key, build_datasource_key, query_hash


class TestBuildDatasourceKey:
    def test_dialect_only_today(self) -> None:
        """With global per-dialect connections, the datasource is the dialect."""
        assert build_datasource_key("postgres") == "postgres"

    def test_principal_distinguishes(self) -> None:
        """A principal (future SSO / per-tenant connections) splits the key."""
        a = build_datasource_key("postgres")
        b = build_datasource_key("postgres", principal="tenant-1")
        c = build_datasource_key("postgres", principal="tenant-2")
        assert a != b != c
        assert b != c


class TestBuildCacheKey:
    def test_identical_sql_same_key(self) -> None:
        sql = "SELECT a FROM t GROUP BY a"
        a = build_cache_key(datasource="ds", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(datasource="ds", model_id="m", dialect="postgres", sql=sql)
        assert a == b

    def test_same_datasource_shares_across_sessions(self) -> None:
        """The whole point of v3: no session_id, so identical queries from
        different sessions collide onto one shared key."""
        sql = "SELECT a FROM t"
        a = build_cache_key(datasource="postgres", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(datasource="postgres", model_id="m", dialect="postgres", sql=sql)
        assert a == b

    def test_different_datasource_different_key(self) -> None:
        """Different data sources (e.g. per-tenant connections) stay isolated."""
        sql = "SELECT a FROM t"
        a = build_cache_key(datasource="postgres", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(
            datasource=build_datasource_key("postgres", principal="tenant-2"),
            model_id="m",
            dialect="postgres",
            sql=sql,
        )
        assert a != b

    def test_different_dialect_different_key(self) -> None:
        sql = "SELECT a FROM t"
        a = build_cache_key(datasource="ds", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(datasource="ds", model_id="m", dialect="snowflake", sql=sql)
        assert a != b

    def test_different_model_different_key(self) -> None:
        sql = "SELECT a FROM t"
        a = build_cache_key(datasource="ds", model_id="m1", dialect="postgres", sql=sql)
        b = build_cache_key(datasource="ds", model_id="m2", dialect="postgres", sql=sql)
        assert a != b

    def test_whitespace_normalized(self) -> None:
        """Trailing semicolons + run-of-whitespace collapse to the same key."""
        a = build_cache_key(
            datasource="ds", model_id="m", dialect="postgres", sql="SELECT a FROM t"
        )
        b = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="postgres",
            sql="  SELECT   a\n  FROM\tt  ; ",
        )
        assert a == b

    def test_different_sql_different_key(self) -> None:
        """The whole point — different compiled SQL → different key."""
        a = build_cache_key(
            datasource="ds", model_id="m", dialect="postgres", sql="SELECT a FROM t"
        )
        b = build_cache_key(
            datasource="ds", model_id="m", dialect="postgres", sql="SELECT b FROM t"
        )
        assert a != b

    def test_whitespace_inside_string_literal_is_significant(self) -> None:
        """``'A  B'`` and ``'A B'`` must NOT collide — they're different values."""
        a = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="postgres",
            sql="SELECT * FROM t WHERE name = 'A  B'",
        )
        b = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="postgres",
            sql="SELECT * FROM t WHERE name = 'A B'",
        )
        assert a != b

    def test_whitespace_inside_double_quoted_identifier_is_significant(self) -> None:
        """ANSI ``"Order  Id"`` and ``"Order Id"`` are different columns."""
        a = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="postgres",
            sql='SELECT "Order  Id" FROM t',
        )
        b = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="postgres",
            sql='SELECT "Order Id" FROM t',
        )
        assert a != b

    def test_whitespace_inside_backtick_identifier_is_significant(self) -> None:
        """MySQL/BigQuery/Databricks backtick identifiers with internal spaces."""
        a = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="bigquery",
            sql="SELECT `Order  Id` FROM t",
        )
        b = build_cache_key(
            datasource="ds",
            model_id="m",
            dialect="bigquery",
            sql="SELECT `Order Id` FROM t",
        )
        assert a != b

    def test_legacy_query_arg_still_works(self) -> None:
        """``query=`` fallback for callers mid-migration."""
        q = {"select": {"dimensions": ["A"], "measures": ["B"]}}
        a = build_cache_key(datasource="ds", model_id="m", dialect="postgres", query=q)
        b = build_cache_key(datasource="ds", model_id="m", dialect="postgres", query=q)
        assert a == b

    def test_missing_both_args_raises(self) -> None:
        with pytest.raises(ValueError, match="either sql or query"):
            build_cache_key(datasource="ds", model_id="m", dialect="postgres")

    def test_key_length_32(self) -> None:
        key = build_cache_key(datasource="ds", model_id="m", dialect="postgres", sql="SELECT 1")
        assert len(key) == 32


class TestQueryHash:
    def test_query_hash_stable(self) -> None:
        sql = "SELECT a FROM t"
        assert query_hash(sql=sql) == query_hash(sql=sql)

    def test_query_hash_length(self) -> None:
        assert len(query_hash(sql="SELECT 1")) == 16

    def test_legacy_query_arg(self) -> None:
        q = {"select": {"dimensions": ["A"]}}
        assert query_hash(query=q) == query_hash(query=q)
