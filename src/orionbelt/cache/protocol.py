"""Cache backend contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass
class CachedResult:
    """A cache hit envelope.

    ``payload`` is the raw bytes the backend wrote on ``set`` (typically a
    gzip'd Arrow IPC blob of row data produced by
    ``orionbelt.cache.result_codec``). The caller decodes it for rows. The
    result's column schema (``columns`` — ``[{name, type, format}]``) and
    ``row_count`` are stored alongside the blob so a caller can rebuild the
    response envelope — and serve a raw-arrow hit verbatim — without decoding
    the payload. ``cached_at`` is when the entry was first written;
    ``ttl_remaining_seconds`` is the time left before expiry on ``get``.
    """

    payload: bytes
    cached_at: datetime
    ttl_remaining_seconds: int
    physical_tables: list[str]
    row_count: int = 0
    columns: list[dict[str, Any]] | None = None


@dataclass
class CacheStats:
    """Lightweight summary of cache state for observability."""

    backend: str
    entry_count: int = 0
    total_size_bytes: int = 0
    max_size_bytes: int = 0
    hit_count_total: int = 0
    miss_count_total: int = 0
    hit_rate: float = 0.0
    oldest_entry: str | None = None
    next_sweep_at: str | None = None
    tracked_physical_tables: int = 0
    heartbeat_invalidations_total: int = 0


class Cache(Protocol):
    """Backend Protocol for the freshness-driven result cache."""

    backend_name: str

    async def get(self, key: str) -> CachedResult | None:
        """Look up a cached entry, lazy-expiring on read."""

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
        """Store an entry, replacing any prior content for the same key.

        ``columns_json`` is the result's column schema (``[{name, type,
        format}]``) as a JSON string, stored alongside the payload so a hit can
        rebuild the response without decoding the blob.
        """

    async def delete(self, key: str) -> None:
        """Drop a single entry by key."""

    async def delete_datasource(self, datasource: str) -> int:
        """Drop every entry for a data source. Returns the count.

        Used to evict a tenant / connection's cache when its credentials or
        connection target change. With global per-dialect connections the
        datasource is the dialect.
        """

    async def invalidate_table(self, table_ref: str) -> int:
        """Drop every entry whose dependency set includes the physical table.

        ``table_ref`` is a ``"DATABASE.SCHEMA.CODE"`` string. Returns the
        count of invalidated entries.
        """

    async def stats(self) -> CacheStats:
        """Return summary statistics."""

    async def sweep_once(self) -> tuple[int, int]:
        """Run one TTL + capacity eviction pass.

        Returns ``(ttl_evicted, capacity_evicted)``. Backends that don't store
        anything (e.g. noop) return ``(0, 0)``.
        """

    async def clear(self) -> int:
        """Drop every entry regardless of TTL or dependencies.

        Returns the number of entries removed. Counters (hits/misses) are
        preserved as historical telemetry.
        """

    async def record_hit(self, key: str) -> None:
        """Increment hit counters. Fire-and-forget; cheap when noop."""

    async def warmup(self) -> None:
        """Exercise the read path once at startup so the first hit isn't cold.

        Default no-op (inherited by backends that have no cold path). A
        disk-backed cache overrides this to warm its metadata queries and
        off-loop blob read. Implementations MUST NOT mutate the hit/miss
        counters or leave a persisted entry, so ``stats()`` stays clean.
        """

    async def shutdown(self) -> None:
        """Release resources (file handles, sweep tasks). Idempotent."""
