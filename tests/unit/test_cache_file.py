"""Tests for FileCache + heartbeat invalidation."""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
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

    async def test_an_entry_expires_by_the_moment_it_is_given(self, cache: FileCache) -> None:
        by = datetime.now(UTC) + timedelta(seconds=60)
        await cache.set(
            "k1",
            b"x",
            ttl_seconds=300,
            physical_tables=["t"],
            datasource="s",
            model_id="m",
            query_hash="h",
            dialect="d",
            row_count=0,
            expires_by=by,
        )
        got = await cache.get("k1")
        assert got is not None
        assert got.expires_at == by
        assert got.ttl_remaining_seconds <= 60

    async def test_an_entry_past_its_expiry_is_not_stored(self, cache: FileCache) -> None:
        await cache.set(
            "k1",
            b"x",
            ttl_seconds=300,
            physical_tables=["t"],
            datasource="s",
            model_id="m",
            query_hash="h",
            dialect="d",
            row_count=0,
            expires_by=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert await cache.get("k1") is None
        assert (await cache.stats()).entry_count == 0

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
        assert (await cache.stats()).heartbeat_invalidations_total == 2

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

    async def test_hits_misses_accumulate_then_flush(self, cache: FileCache) -> None:
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

        # stats() reports the totals without writing them.
        stats = await cache.stats()
        assert stats.hit_count_total == 3
        assert stats.miss_count_total == 2
        assert stats.hit_rate == pytest.approx(0.6)
        assert cache._pending_counters.get("hits") == 3

        # A sweep flushes them into DuckDB; the totals stay, not counted twice.
        await cache.sweep_once()
        assert cache._pending_counters.get("hits", 0) == 0
        assert cache._pending_counters.get("misses", 0) == 0
        stored = dict(cache._meta.execute("SELECT name, value FROM cache_counters").fetchall())
        assert (stored["hits"], stored["misses"]) == (3, 2)
        stats = await cache.stats()
        assert (stats.hit_count_total, stats.miss_count_total) == (3, 2)

    async def test_totals_survive_a_reopen(self, tmp_path: Path) -> None:
        def open_cache() -> FileCache:
            return FileCache(
                cache_dir=str(tmp_path),
                max_value_bytes=1024,
                max_disk_bytes=1024 * 1024,
                max_ttl_seconds=3600,
                sweep_interval_seconds=60,
            )

        first = open_cache()
        assert await first.get("absent") is None
        await first.shutdown()  # flushes
        second = open_cache()
        try:
            assert (await second.stats()).miss_count_total == 1
        finally:
            await second.shutdown()


class TestStatsOnAnyThread:
    """stats() runs on any thread without the lock the other operations hold."""

    def test_stats_read_while_another_thread_holds_the_lock(self, tmp_path: Path) -> None:
        cache = FileCache(
            cache_dir=str(tmp_path),
            max_value_bytes=1024,
            max_disk_bytes=1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=60,
        )
        asyncio.run(
            cache.set(
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
        )
        assert asyncio.run(cache.get("k1")) is not None
        found: list[object] = []
        # As a slow operation on another thread would: the metrics read
        # stats() from their scrape thread while the API works on the cache.
        with cache._lock:
            reader = threading.Thread(target=lambda: found.append(asyncio.run(cache.stats())))
            reader.start()
            reader.join(timeout=5)
            assert not reader.is_alive(), "stats() waited for the cache's lock"
        stats = found[0]
        assert getattr(stats, "entry_count", None) == 1
        assert getattr(stats, "hit_count_total", None) == 1
        asyncio.run(cache.shutdown())
        # Shut down, it reads nothing and still answers.
        assert asyncio.run(cache.stats()).entry_count == 0


class TestWarmup:
    """warmup() drives the full hit path without polluting stats or entries."""

    async def test_warmup_leaves_stats_and_entries_clean(self, cache: FileCache) -> None:
        await cache.warmup()

        # No synthetic hit/miss recorded, no lingering entry or table dep.
        assert cache._pending_counters.get("hits", 0) == 0
        assert cache._pending_counters.get("misses", 0) == 0
        stats = await cache.stats()
        assert stats.hit_count_total == 0
        assert stats.miss_count_total == 0
        assert stats.entry_count == 0
        assert stats.tracked_physical_tables == 0

        # The reserved key is gone (evicted), so a later lookup is a normal miss.
        assert await cache.get("__obsl_warmup__") is None

    async def test_warmup_is_idempotent(self, cache: FileCache) -> None:
        await cache.warmup()
        await cache.warmup()
        stats = await cache.stats()
        assert stats.entry_count == 0


def _store(cache: FileCache, key: str) -> Any:
    return cache.set(
        key,
        b"payload",
        ttl_seconds=600,
        physical_tables=["WH.PUB.T"],
        datasource="s",
        model_id="m",
        query_hash="h",
        dialect="duckdb",
        row_count=1,
    )


class TestSharedByThreads:
    """The threads using the cache share its metadata connection.

    The API's event loop and Flight's handlers, each on a loop of its own,
    call the same cache. DuckDB refuses a call on a connection while another
    thread's runs on it, so a statement's rows are read before the lock is
    let go, and a transaction's ROLLBACK runs before it too.
    """

    def test_no_lookup_or_write_is_lost(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = FileCache(
            cache_dir=str(tmp_path),
            max_value_bytes=1024 * 1024,
            max_disk_bytes=64 * 1024 * 1024,
            max_ttl_seconds=3600,
            sweep_interval_seconds=3600,
        )
        asyncio.run(_store(cache, "hot"))
        outcomes: collections.Counter[str] = collections.Counter()

        def reader() -> None:
            loop = asyncio.new_event_loop()
            try:
                for _ in range(300):
                    hit = loop.run_until_complete(cache.get("hot"))
                    outcomes["hit" if hit is not None else "miss"] += 1
            finally:
                loop.close()

        def writer() -> None:
            loop = asyncio.new_event_loop()
            try:
                for n in range(150):
                    loop.run_until_complete(_store(cache, f"k{n}"))
            finally:
                loop.close()

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads.append(threading.Thread(target=writer))
        with caplog.at_level(logging.WARNING, logger="orionbelt.cache.file"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        try:
            assert outcomes == {"hit": 900}, outcomes  # the key is stored and fresh
            assert asyncio.run(cache.stats()).entry_count == 151  # every write kept
            assert not caplog.records, [r.getMessage() for r in caplog.records[:3]]
        finally:
            asyncio.run(cache.shutdown())

    async def test_a_failed_transaction_rolls_back_under_the_lock(self, cache: FileCache) -> None:
        connection = cache._meta
        seen: list[tuple[str, bool]] = []

        class Recording:
            """The connection, noting whether the lock is held at each statement."""

            def execute(self, sql: str, params: Any = None) -> Any:
                seen.append((sql.split()[0], cache._lock.locked()))
                if "INTO cache_entry_tables" in sql:
                    raise duckdb.Error("injected failure")
                return connection.execute(sql, params or [])

            def __getattr__(self, name: str) -> Any:
                return getattr(connection, name)

        cache._meta = Recording()  # type: ignore[assignment]
        try:
            await _store(cache, "k1")
        finally:
            cache._meta = connection
        assert seen[0] == ("BEGIN", True) and seen[-1] == ("ROLLBACK", True), seen
        assert all(held for _, held in seen), seen
        assert await cache.get("k1") is None  # nothing of it stayed
        assert (await cache.stats()).entry_count == 0
