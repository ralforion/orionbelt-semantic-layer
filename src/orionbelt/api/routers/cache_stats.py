"""Cache stats / sweep endpoints — under /v1/cache."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orionbelt.api.deps import get_cache, get_session_manager
from orionbelt.api.schemas import (
    CacheClearResponse,
    CacheStatsResponse,
    CacheSweepResponse,
    CompilationCacheClearResponse,
    CompilationCacheStatsResponse,
)
from orionbelt.cache.protocol import Cache
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


@router.get("/stats", response_model=CacheStatsResponse, tags=["cache"])
async def get_cache_stats(
    cache: Cache = Depends(get_cache),  # noqa: B008
) -> CacheStatsResponse:
    """Return summary statistics for the result cache.

    Always responds — when ``CACHE_BACKEND=noop`` the response shows
    ``backend: "noop"`` with zero counters.
    """
    s = await cache.stats()
    return CacheStatsResponse(
        backend=s.backend,
        entry_count=s.entry_count,
        total_size_bytes=s.total_size_bytes,
        max_size_bytes=s.max_size_bytes,
        hit_count_total=s.hit_count_total,
        miss_count_total=s.miss_count_total,
        hit_rate=s.hit_rate,
        oldest_entry=s.oldest_entry,
        next_sweep_at=s.next_sweep_at,
        tracked_physical_tables=s.tracked_physical_tables,
        heartbeat_invalidations_total=s.heartbeat_invalidations_total,
    )


@router.post("/sweep", response_model=CacheSweepResponse, tags=["cache"])
async def trigger_cache_sweep(
    cache: Cache = Depends(get_cache),  # noqa: B008
) -> CacheSweepResponse:
    """Trigger a TTL + capacity eviction pass on demand.

    Equivalent to one tick of the periodic sweeper. Safe to call at any
    time. When ``CACHE_BACKEND=noop`` returns zero counts.
    """
    ttl_evicted, capacity_evicted = await cache.sweep_once()
    return CacheSweepResponse(
        backend=cache.backend_name,
        ttl_evicted=ttl_evicted,
        capacity_evicted=capacity_evicted,
    )


@router.post("/clear", response_model=CacheClearResponse, tags=["cache"])
async def clear_cache(
    cache: Cache = Depends(get_cache),  # noqa: B008
) -> CacheClearResponse:
    """Drop every cached entry regardless of TTL or freshness contract.

    Counters (hits/misses, heartbeat invalidations) are preserved. When
    ``CACHE_BACKEND=noop`` returns zero.
    """
    entries_cleared = await cache.clear()
    return CacheClearResponse(
        backend=cache.backend_name,
        entries_cleared=entries_cleared,
    )


@router.get("/compilation", response_model=CompilationCacheStatsResponse, tags=["cache"])
async def get_compilation_cache_stats(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> CompilationCacheStatsResponse:
    """Return statistics for the compilation cache (compiled SQL, not rows).

    Always responds; ``enabled`` is false when ``COMPILE_CACHE_MAX_ENTRIES=0``.
    """
    s = mgr.compilation_cache.stats()
    return CompilationCacheStatsResponse(
        enabled=s.enabled,
        entries=s.entries,
        bytes=s.bytes,
        max_entries=s.max_entries,
        max_bytes=s.max_bytes,
        hits=s.hits,
        misses=s.misses,
        bypasses=s.bypasses,
        evictions=s.evictions,
    )


@router.post("/compilation/clear", response_model=CompilationCacheClearResponse, tags=["cache"])
async def clear_compilation_cache(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> CompilationCacheClearResponse:
    """Drop every compiled query. The result cache is not touched; counters are kept."""
    return CompilationCacheClearResponse(entries_cleared=mgr.compilation_cache.clear())
