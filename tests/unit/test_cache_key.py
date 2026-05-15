"""Tests for cache.key — deterministic key construction.

v2 (2026-05): keys hash on compiled SQL strings, not QueryObject dicts.
Two callers that compile to the same SQL get the same key, regardless
of how they assembled it (OBSQL, QueryObject, OBML YAML).
"""

from __future__ import annotations

import pytest

from orionbelt.cache.key import build_cache_key, query_hash


class TestBuildCacheKey:
    def test_identical_sql_same_key(self) -> None:
        sql = "SELECT a FROM t GROUP BY a"
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(session_id="s", model_id="m", dialect="postgres", sql=sql)
        assert a == b

    def test_different_session_different_key(self) -> None:
        sql = "SELECT a FROM t"
        a = build_cache_key(session_id="s1", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(session_id="s2", model_id="m", dialect="postgres", sql=sql)
        assert a != b

    def test_different_dialect_different_key(self) -> None:
        sql = "SELECT a FROM t"
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", sql=sql)
        b = build_cache_key(session_id="s", model_id="m", dialect="snowflake", sql=sql)
        assert a != b

    def test_different_model_different_key(self) -> None:
        sql = "SELECT a FROM t"
        a = build_cache_key(session_id="s", model_id="m1", dialect="postgres", sql=sql)
        b = build_cache_key(session_id="s", model_id="m2", dialect="postgres", sql=sql)
        assert a != b

    def test_whitespace_normalized(self) -> None:
        """Trailing semicolons + run-of-whitespace collapse to the same key."""
        a = build_cache_key(
            session_id="s", model_id="m", dialect="postgres", sql="SELECT a FROM t"
        )
        b = build_cache_key(
            session_id="s",
            model_id="m",
            dialect="postgres",
            sql="  SELECT   a\n  FROM\tt  ; ",
        )
        assert a == b

    def test_different_sql_different_key(self) -> None:
        """The whole point — different compiled SQL → different key."""
        a = build_cache_key(
            session_id="s", model_id="m", dialect="postgres", sql="SELECT a FROM t"
        )
        b = build_cache_key(
            session_id="s", model_id="m", dialect="postgres", sql="SELECT b FROM t"
        )
        assert a != b

    def test_legacy_query_arg_still_works(self) -> None:
        """``query=`` fallback for callers mid-migration."""
        q = {"select": {"dimensions": ["A"], "measures": ["B"]}}
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q)
        b = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q)
        assert a == b

    def test_missing_both_args_raises(self) -> None:
        with pytest.raises(ValueError, match="either sql or query"):
            build_cache_key(session_id="s", model_id="m", dialect="postgres")

    def test_key_length_32(self) -> None:
        key = build_cache_key(session_id="s", model_id="m", dialect="postgres", sql="SELECT 1")
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
