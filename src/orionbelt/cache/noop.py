"""Disabled cache backend — gets miss, sets no-op."""

from __future__ import annotations

from orionbelt.cache.protocol import Cache, CachedResult, CacheStats


class NoopCache(Cache):
    """Disabled cache: ``get`` always misses, ``set`` is a no-op.

    Selected when ``CACHE_BACKEND=noop`` (the default). Keeps the call sites
    cache-aware without paying for any storage.
    """

    backend_name = "noop"

    async def get(self, key: str) -> CachedResult | None:
        return None

    async def set(
        self,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int,
        physical_tables: list[str],
        datasource: str,
        model_id: str,
        query_hash: str,
        dialect: str,
        row_count: int,
        columns_json: str | None = None,
    ) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def delete_datasource(self, datasource: str) -> int:
        return 0

    async def invalidate_table(self, table_ref: str) -> int:
        return 0

    async def stats(self) -> CacheStats:
        return CacheStats(backend=self.backend_name)

    async def sweep_once(self) -> tuple[int, int]:
        return (0, 0)

    async def clear(self) -> int:
        return 0

    async def record_hit(self, key: str) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None
