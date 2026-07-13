"""Tests for the surface-agnostic cache plan (service.query_execution).

Issue #126: REST, pgwire, and Flight must derive the *same* cache key and
freshness TTL for a given compiled query. They all funnel through
``resolve_cache_plan`` now, so these tests pin the shared contract and guard
against a future cross-surface drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
