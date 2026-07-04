"""Tests for FileCache + heartbeat invalidation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orionbelt.cache.file import FileCache


@pytest.fixture
async def cache(tmp_path: Path):
    fc = FileCache(
        cache_dir=str(tmp_path),
        max_value_bytes=10 * 1024 * 1024,
        max_disk_bytes=50 * 1024 * 1024,
        max_ttl_seconds=3600,
        sweep_interval_seconds=60,
    )
    yield fc
    await fc.shutdown()


class TestFileCacheBasics:
    async def test_get_miss_returns_none(self, cache: FileCache) -> None:
        result = await cache.get("nonexistent")
        assert result is None

    async def test_set_then_get_round_trip(self, cache: FileCache) -> None:
        payload = b"hello-world"
        await cache.set(
            "k1",
            payload,
            ttl_seconds=300,
            physical_tables=["WH.PUB.ORDERS"],
            datasource="sess1",
            model_id="m1",
            query_hash="h",
            dialect="postgres",
            row_count=42,
        )
        got = await cache.get("k1")
        assert got is not None
        assert got.payload == payload
        assert "WH.PUB.ORDERS" in got.physical_tables
        assert got.ttl_remaining_seconds > 0

    async def test_zero_ttl_skips_cache(self, cache: FileCache) -> None:
        await cache.set(
            "k1",
            b"x",
            ttl_seconds=0,
            physical_tables=["t"],
            datasource="s",
            model_id="m",
            query_hash="h",
            dialect="d",
            row_count=0,
        )
        assert await cache.get("k1") is None

    async def test_oversize_payload_skipped(self, tmp_path: Path) -> None:
        fc = FileCache(
            cache_dir=str(tmp_path),
            max_value_bytes=10,
            max_disk_bytes=1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=60,
        )
        try:
            await fc.set(
                "k1",
                b"x" * 100,
                ttl_seconds=300,
                physical_tables=["t"],
                datasource="s",
                model_id="m",
                query_hash="h",
                dialect="d",
                row_count=0,
            )
            assert await fc.get("k1") is None
        finally:
            await fc.shutdown()


class TestInvalidateTable:
    async def test_heartbeat_invalidates_dependent_entries(self, cache: FileCache) -> None:
        # Two cached results both depend on WH.PUB.ORDERS
        for k, tables in [
            ("a", ["WH.PUB.ORDERS"]),
            ("b", ["WH.PUB.ORDERS", "WH.PUB.CUSTOMERS"]),
            ("c", ["WH.PUB.PRODUCTS"]),  # unaffected
        ]:
            await cache.set(
                k,
                b"x",
                ttl_seconds=600,
                physical_tables=tables,
                datasource="s",
                model_id="m",
                query_hash="h",
                dialect="d",
                row_count=1,
            )

        invalidated = await cache.invalidate_table("WH.PUB.ORDERS")
        assert invalidated == 2
        assert await cache.get("a") is None
        assert await cache.get("b") is None
        assert await cache.get("c") is not None

    async def test_invalidate_no_match_returns_zero(self, cache: FileCache) -> None:
        n = await cache.invalidate_table("NEVER.SEEN.TABLE")
        assert n == 0


class TestDatasourceDelete:
    async def test_delete_datasource_drops_only_owned_entries(self, cache: FileCache) -> None:
        for ds, k in [("postgres", "a"), ("postgres", "b"), ("snowflake", "c")]:
            await cache.set(
                k,
                b"x",
                ttl_seconds=600,
                physical_tables=["t"],
                datasource=ds,
                model_id="m",
                query_hash="h",
                dialect="d",
                row_count=1,
            )
        n = await cache.delete_datasource("postgres")
        assert n == 2
        assert await cache.get("a") is None
        assert await cache.get("c") is not None


class TestHeartbeatStorage:
    async def test_record_and_snapshot(self, cache: FileCache) -> None:
        ts = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
        await cache.record_heartbeat("WH.PUB.ORDERS", ts)
        snap = cache.heartbeats_snapshot()
        assert snap.get("WH.PUB.ORDERS") == ts

    async def test_older_heartbeat_ignored(self, cache: FileCache) -> None:
        new_ts = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
        old_ts = datetime(2026, 5, 5, 11, 0, 0, tzinfo=UTC)
        await cache.record_heartbeat("WH.PUB.ORDERS", new_ts)
        await cache.record_heartbeat("WH.PUB.ORDERS", old_ts)
        snap = cache.heartbeats_snapshot()
        assert snap["WH.PUB.ORDERS"] == new_ts


class TestSweep:
    async def test_ttl_eviction(self, tmp_path: Path) -> None:
        fc = FileCache(
            cache_dir=str(tmp_path),
            max_value_bytes=10 * 1024 * 1024,
            max_disk_bytes=50 * 1024 * 1024,
            max_ttl_seconds=10,
            sweep_interval_seconds=3600,
        )
        try:
            await fc.set(
                "expired",
                b"x",
                ttl_seconds=1,
                physical_tables=["t"],
                datasource="s",
                model_id="m",
                query_hash="h",
                dialect="d",
                row_count=1,
            )
            # Force expiry by writing in the past
            import time as _time

            _time.sleep(1.1)
            ttl_evicted, _ = await fc.sweep_once()
            # Sweep should evict the expired entry (1s TTL elapsed)
            assert ttl_evicted >= 1
            assert await fc.get("expired") is None
        finally:
            await fc.shutdown()


class TestDeferredCounters:
    """Hit/miss counters accumulate in memory and flush lazily (no per-hit write)."""

    async def test_hits_misses_accumulate_then_flush_into_stats(self, cache: FileCache) -> None:
        await cache.set(
            "k1",
            b"payload",
            ttl_seconds=300,
            physical_tables=["t"],
            datasource="s",
            model_id="m",
            query_hash="h",
            dialect="d",
            row_count=1,
        )
        # 3 hits + 2 misses; these live in memory, not yet in the DuckDB counters.
        for _ in range(3):
            assert await cache.get("k1") is not None
        for _ in range(2):
            assert await cache.get("absent") is None
        assert cache._pending_counters.get("hits") == 3
        assert cache._pending_counters.get("misses") == 2

        # stats() flushes the pending counters and reports the merged totals.
        stats = await cache.stats()
        assert stats.hit_count_total == 3
        assert stats.miss_count_total == 2
        assert stats.hit_rate == pytest.approx(0.6)
        # Drained after flush — no double counting on the next read.
        assert cache._pending_counters.get("hits", 0) == 0
        assert cache._pending_counters.get("misses", 0) == 0
