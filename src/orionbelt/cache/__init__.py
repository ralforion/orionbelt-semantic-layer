"""Freshness-driven result cache.

See ``design/PLAN_freshness_driven_cache.md``. Public surface:

- :class:`Cache` Protocol — backend contract.
- :class:`CachedResult` — return shape from ``get``.
- :class:`NoopCache` — disabled backend (default).
- :class:`FileCache` — DuckDB metadata + Parquet result files.
- :func:`build_cache` — factory selecting backend by env.
- :func:`build_cache_key` — deterministic key from datasource/model/dialect/query.
- :func:`build_datasource_key` — identity of the physical data source (shared scope).
- :class:`TtlResult` — outcome of TTL composition over physical tables.
- :func:`compute_effective_ttl` — combine source contracts into a single TTL.
"""

from __future__ import annotations

from orionbelt.cache.determinism import is_nondeterministic_sql
from orionbelt.cache.factory import build_cache
from orionbelt.cache.key import build_cache_key, build_datasource_key, query_hash
from orionbelt.cache.noop import NoopCache
from orionbelt.cache.protocol import Cache, CachedResult
from orionbelt.cache.ttl import (
    NoCacheReason,
    TtlComputation,
    TtlResult,
    compute_effective_ttl,
)

__all__ = [
    "Cache",
    "CachedResult",
    "NoCacheReason",
    "NoopCache",
    "TtlComputation",
    "TtlResult",
    "build_cache",
    "build_cache_key",
    "build_datasource_key",
    "compute_effective_ttl",
    "is_nondeterministic_sql",
    "query_hash",
]
