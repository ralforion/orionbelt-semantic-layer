"""Tests for the surface-agnostic cache plan (service.query_execution).

Issue #126: REST, pgwire, and Flight must derive the *same* cache key and
freshness TTL for a given compiled query. They all funnel through
``resolve_cache_plan`` now, so these tests pin the shared contract and guard
against a future cross-surface drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from orionbelt.cache.key import build_cache_key, build_datasource_key
from orionbelt.cache.ttl import NoCacheReason
from orionbelt.service.query_execution import resolve_cache_plan


class _FakeStore:
    """A store with no refresh contracts (every table UNKNOWN freshness)."""

    def refresh_contracts(self, model_id: str) -> dict[str, Any]:
        return {}


class _FakeCache:
    """A cache with no heartbeat snapshot (getattr falls through to None)."""


@dataclass
class _FakeCacheConfig:
    min_ttl_seconds: int = 5
    max_ttl_seconds: int = 86400
    # ``default_ttl`` makes an unknown-freshness query cacheable, so the happy
    # path exercises key + TTL together. Switch to ``no_cache`` to test the
    # not-cacheable branch.
    unknown_policy: str = "default_ttl"
    unknown_default_ttl_seconds: int = 300


def _plan(**overrides: Any):
    kwargs: dict[str, Any] = {
        "store": _FakeStore(),
        "model_id": "m",
        "dialect": "postgres",
        "sql": "SELECT a FROM t GROUP BY a",
        "physical_tables": ["t"],
        "cache": _FakeCache(),
        "cache_config": _FakeCacheConfig(),
    }
    kwargs.update(overrides)
    return resolve_cache_plan(**kwargs)


class TestCrossSurfaceKey:
    def test_same_query_same_key_across_surfaces(self) -> None:
        """REST/pgwire pass a pre-resolved datasource; Flight lets it default.
        Both must land on the identical key for one compiled query."""
        sql = "SELECT a FROM t GROUP BY a"
        # REST / pgwire style: datasource resolved by the caller.
        rest = _plan(sql=sql, datasource=build_datasource_key("postgres"))
        # Flight style: datasource omitted, defaulted inside the plan.
        flight = _plan(sql=sql)
        assert rest.cache_key == flight.cache_key
        # ... and both equal the canonical key.
        assert rest.cache_key == build_cache_key(
            datasource="postgres", model_id="m", dialect="postgres", sql=sql
        )
        assert rest.cacheable and flight.cacheable

    def test_datasource_echoed_back(self) -> None:
        plan = _plan()
        assert plan.datasource == build_datasource_key("postgres")

    def test_different_dialect_different_key(self) -> None:
        pg = _plan(dialect="postgres")
        sf = _plan(dialect="snowflake")
        assert pg.cache_key != sf.cache_key


class TestCacheability:
    def test_nondeterministic_sql_is_never_cacheable(self) -> None:
        plan = _plan(sql="SELECT NOW()", physical_tables=[])
        assert not plan.cacheable
        assert plan.cache_key is None
        assert plan.ttl_outcome.no_cache_reason == NoCacheReason.NON_DETERMINISTIC_SQL

    def test_unknown_freshness_no_cache_policy(self) -> None:
        """Key is still derived, but the TTL says don't cache — so both the
        read and write gates on the callers short-circuit."""
        plan = _plan(cache_config=_FakeCacheConfig(unknown_policy="no_cache"))
        assert not plan.cacheable
        assert plan.cache_key is not None
        assert plan.ttl_outcome.no_cache_reason == NoCacheReason.UNKNOWN_FRESHNESS

    def test_cacheable_when_default_ttl_policy(self) -> None:
        plan = _plan()
        assert plan.cacheable
        assert plan.ttl_outcome.ttl is not None
        assert plan.ttl_outcome.ttl.seconds == 300


_TODAY_MODEL_YAML = """\
version: 1.0
settings:
{settings}
dataObjects:
  Orders:
    code: orders
    columns:
      Order ID: {{code: id, abstractType: string}}
      Ordered On: {{code: ordered_on, abstractType: date}}
dimensions:
  Ordered On: {{dataObject: Orders, column: Ordered On, resultType: date}}
measures:
  Order Count:
    columns: [{{dataObject: Orders, column: Order ID}}]
    aggregation: count
    resultType: int
  Days Since Last:
    expression: "date_diff('day', {{[Orders].[Ordered On]}}, current_date())"
    aggregation: min
    resultType: int
"""

_LAST_WEEK = {
    "select": {"dimensions": ["Ordered On"], "measures": ["Order Count"]},
    "where": [
        {"field": "Ordered On", "op": "relative", "value": {"unit": "day", "count": 7}},
    ],
}
_DAYS_SINCE = {"select": {"dimensions": ["Ordered On"], "measures": ["Days Since Last"]}}


class TestTodayIsNeverCached:
    """A query on today, as each dialect compiles it, is never cached.

    The cache keys on the SQL, so a cached "last 7 days" would answer with
    yesterday's window after midnight. MySQL's ``CURDATE()`` (a relative
    filter) and ``UTC_TIMESTAMP()`` (today in ``queryTimezone``) were cached
    for the default TTL.
    """

    @pytest.mark.parametrize("query", [_LAST_WEEK, _DAYS_SINCE], ids=["relative", "current_date"])
    @pytest.mark.parametrize(
        "settings",
        ["  defaultDialect: duckdb", "  queryTimezone: America/New_York"],
        ids=["session_zone", "query_zone"],
    )
    @pytest.mark.parametrize(
        "dialect",
        [
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ],
    )
    def test_today_is_not_cacheable(
        self, dialect: str, settings: str, query: dict[str, Any]
    ) -> None:
        from orionbelt.compiler.pipeline import CompilationPipeline
        from orionbelt.models.query import QueryObject
        from orionbelt.parser.loader import TrackedLoader
        from orionbelt.parser.resolver import ReferenceResolver

        raw, source_map = TrackedLoader().load_string(_TODAY_MODEL_YAML.format(settings=settings))
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        sql = CompilationPipeline().compile(QueryObject.model_validate(query), model, dialect).sql

        plan = _plan(dialect=dialect, sql=sql, physical_tables=["orders"])

        assert not plan.cacheable, sql
        assert plan.ttl_outcome.no_cache_reason == NoCacheReason.NON_DETERMINISTIC_SQL
