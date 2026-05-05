"""Tests for cache.key — deterministic key construction + query normalization."""

from __future__ import annotations

from orionbelt.cache.key import build_cache_key, query_hash


class TestBuildCacheKey:
    def test_identical_queries_same_key(self) -> None:
        q = {"select": {"dimensions": ["A"], "measures": ["B"]}}
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q)
        b = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q)
        assert a == b

    def test_different_session_different_key(self) -> None:
        q = {"select": {"dimensions": ["A"], "measures": ["B"]}}
        a = build_cache_key(session_id="s1", model_id="m", dialect="postgres", query=q)
        b = build_cache_key(session_id="s2", model_id="m", dialect="postgres", query=q)
        assert a != b

    def test_different_dialect_different_key(self) -> None:
        q = {"select": {"dimensions": ["A"], "measures": ["B"]}}
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q)
        b = build_cache_key(session_id="s", model_id="m", dialect="snowflake", query=q)
        assert a != b

    def test_dimension_order_normalized(self) -> None:
        q1 = {"select": {"dimensions": ["A", "B"], "measures": ["X"]}}
        q2 = {"select": {"dimensions": ["B", "A"], "measures": ["X"]}}
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q1)
        b = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q2)
        assert a == b

    def test_in_op_value_order_normalized(self) -> None:
        q1 = {
            "select": {"dimensions": ["A"], "measures": ["X"]},
            "where": [{"field": "A", "op": "in", "value": ["x", "y", "z"]}],
        }
        q2 = {
            "select": {"dimensions": ["A"], "measures": ["X"]},
            "where": [{"field": "A", "op": "in", "value": ["z", "x", "y"]}],
        }
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q1)
        b = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q2)
        assert a == b

    def test_order_by_preserved(self) -> None:
        q1 = {
            "select": {"dimensions": ["A"], "measures": ["X"]},
            "order_by": [{"field": "X", "direction": "desc"}],
        }
        q2 = {
            "select": {"dimensions": ["A"], "measures": ["X"]},
            "order_by": [{"field": "X", "direction": "asc"}],
        }
        a = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q1)
        b = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q2)
        assert a != b

    def test_key_length_32(self) -> None:
        q = {"select": {"dimensions": [], "measures": []}}
        key = build_cache_key(session_id="s", model_id="m", dialect="postgres", query=q)
        assert len(key) == 32


class TestQueryHash:
    def test_query_hash_stable(self) -> None:
        q = {"select": {"dimensions": ["A"], "measures": ["B"]}}
        assert query_hash(q) == query_hash(q)

    def test_query_hash_length(self) -> None:
        q = {"select": {"dimensions": [], "measures": []}}
        assert len(query_hash(q)) == 16
