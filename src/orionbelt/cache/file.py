"""File-backed cache: DuckDB metadata + Arrow IPC result files.

See ``design/PLAN_freshness_driven_cache.md`` §7, §12, §13 and
``design/PLAN_arrow_cache.md`` §3. Layout:

    {CACHE_DIR}/
      meta.duckdb                       — control plane (entries, deps, heartbeats)
      results/{prefix}/{ds}/{key}.arrow  — payloads ({ds} = datasource)

Payloads are gzip'd Arrow IPC streams (``orionbelt.cache.result_codec``); the
backend treats them as opaque bytes. The ``.arrow`` extension marks them as
OBSL-internal — do not read them as Parquet. The metadata DB is opened once and
protected by a lock; DuckDB is not async-safe. Filesystem writes happen via
temp+rename for atomicity.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import duckdb

from orionbelt.cache.protocol import Cache, CachedResult, CacheStats

logger = logging.getLogger(__name__)


def _read_bytes(path: str) -> bytes:
    """Read a payload file's bytes (offloaded to a worker thread by ``get``)."""
    with open(path, "rb") as f:
        return f.read()


_SCHEMA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS cache_entries (
        cache_key      VARCHAR PRIMARY KEY,
        datasource     VARCHAR NOT NULL,
        model_id       VARCHAR NOT NULL,
        query_hash     VARCHAR NOT NULL,
        dialect        VARCHAR NOT NULL,
        file_path      VARCHAR NOT NULL,
        row_count      BIGINT,
        columns_json   VARCHAR,
        size_bytes     BIGINT,
        created_at     TIMESTAMP WITH TIME ZONE NOT NULL,
        expires_at     TIMESTAMP WITH TIME ZONE NOT NULL,
        last_hit_at    TIMESTAMP WITH TIME ZONE,
        hit_count      BIGINT DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_entry_tables (
        cache_key VARCHAR,
        table_ref VARCHAR,
        PRIMARY KEY (cache_key, table_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS table_heartbeats (
        table_ref      VARCHAR PRIMARY KEY,
        last_heartbeat TIMESTAMP WITH TIME ZONE NOT NULL,
        received_at    TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_counters (
        name  VARCHAR PRIMARY KEY,
        value BIGINT  NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_datasource ON cache_entries(datasource)",
    "CREATE INDEX IF NOT EXISTS idx_expires    ON cache_entries(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_last_hit   ON cache_entries(last_hit_at)",
    "CREATE INDEX IF NOT EXISTS idx_table_ref  ON cache_entry_tables(table_ref)",
]


class FileCache(Cache):
    """DuckDB metadata + Parquet payload cache backend."""

    backend_name = "file"

    def __init__(
        self,
        *,
        cache_dir: str,
        max_value_bytes: int,
        max_disk_bytes: int,
        max_ttl_seconds: int,
        sweep_interval_seconds: int,
    ) -> None:
        self._cache_dir = os.path.abspath(cache_dir)
        self._results_dir = os.path.join(self._cache_dir, "results")
        self._meta_path = os.path.join(self._cache_dir, "meta.duckdb")
        self._max_value_bytes = max_value_bytes
        self._max_disk_bytes = max_disk_bytes
        self._max_ttl_seconds = max_ttl_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._lock = threading.Lock()
        self._sweep_task: asyncio.Task[None] | None = None
        self._next_sweep_at: float | None = None
        # Hit/miss counts accumulate in memory and flush to DuckDB lazily (on
        # sweep / stats / shutdown). This keeps the read-hot ``get`` path off a
        # per-hit DuckDB UPDATE under the global lock — the counter write was a
        # synchronous OLTP point-write on an OLAP engine on every cache hit.
        self._counter_lock = threading.Lock()
        self._pending_counters: dict[str, int] = {}

        os.makedirs(self._results_dir, exist_ok=True)
        self._meta = duckdb.connect(self._meta_path)
        self._migrate_schema()
        for ddl in _SCHEMA_DDL:
            self._meta.execute(ddl)
        self._meta.execute(
            "INSERT INTO cache_counters VALUES ('hits', 0), ('misses', 0), "
            "('heartbeat_invalidations', 0) ON CONFLICT DO NOTHING"
        )

    # -- internals ----------------------------------------------------------

    def _migrate_schema(self) -> None:
        """Bring a pre-existing meta DB up to the current schema.

        v3 (2026-06): the cache scope changed from per-session to
        per-datasource (see ``orionbelt.cache.key``). Rename the legacy
        ``session_id`` column on caches created before the change so the new
        DDL and queries find it. Stored rows survive, but their keys are
        recomputed under the bumped ``KEY_VERSION`` and simply miss + age out.
        """
        try:
            cols = {
                row[0]
                for row in self._meta.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'cache_entries'"
                ).fetchall()
            }
        except Exception:
            return
        if "session_id" in cols and "datasource" not in cols:
            with contextlib.suppress(Exception):
                self._meta.execute(
                    "ALTER TABLE cache_entries RENAME COLUMN session_id TO datasource"
                )
            with contextlib.suppress(Exception):
                self._meta.execute("DROP INDEX IF EXISTS idx_session")
        # v4 (2026-07): the cached blob holds only row data; the result column
        # schema now rides in this column so a hit rebuilds without decoding.
        if "columns_json" not in cols:
            with contextlib.suppress(Exception):
                self._meta.execute("ALTER TABLE cache_entries ADD COLUMN columns_json VARCHAR")

    def _exec(
        self, sql: str, params: list[object] | tuple[object, ...] | None = None
    ) -> duckdb.DuckDBPyConnection:
        with self._lock:
            return self._meta.execute(sql, params or [])

    def _bump(self, name: str, by: int = 1) -> None:
        try:
            self._exec("UPDATE cache_counters SET value = value + ? WHERE name = ?", [by, name])
        except Exception:
            logger.debug("cache counter bump failed", exc_info=True)

    def _count(self, name: str, by: int = 1) -> None:
        """Accumulate a counter in memory (flushed to DuckDB by :meth:`_flush_counters`)."""
        with self._counter_lock:
            self._pending_counters[name] = self._pending_counters.get(name, 0) + by

    def _flush_counters(self) -> None:
        """Drain the in-memory counters into the DuckDB ``cache_counters`` table."""
        with self._counter_lock:
            pending = {k: v for k, v in self._pending_counters.items() if v}
            self._pending_counters.clear()
        for name, by in pending.items():
            self._bump(name, by)

    def _file_path(self, datasource: str, key: str) -> str:
        safe = (datasource or "_").replace(os.sep, "_")
        prefix = safe[:2]
        directory = os.path.join(self._results_dir, prefix, safe)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"{key}.arrow")

    def _unlink(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("cache unlink failed for %s: %s", path, exc)

    # -- Cache Protocol -----------------------------------------------------

    async def get(self, key: str) -> CachedResult | None:
        # The DuckDB metadata connection is single-threaded (touched only on the
        # event loop, serialized by ``self._lock``), so its lookups stay here.
        # Only the potentially-large payload file read is offloaded, and the
        # per-hit counter is now an in-memory increment (no DuckDB write) — the
        # two things that previously blocked the loop on a hit.
        try:
            row = self._exec(
                "SELECT file_path, expires_at, created_at, row_count, columns_json "
                "FROM cache_entries WHERE cache_key = ?",
                [key],
            ).fetchone()
        except Exception as exc:
            logger.warning("cache.get metadata error: %s", exc)
            return None

        if row is None:
            self._count("misses")
            return None

        file_path, expires_at, created_at, row_count, columns_json = row
        now = datetime.now(UTC)
        if expires_at is None or expires_at <= now:
            await self._evict(key, file_path)
            self._count("misses")
            return None

        try:
            payload = await asyncio.to_thread(_read_bytes, file_path)
        except FileNotFoundError:
            await self._evict(key, file_path)
            self._count("misses")
            return None
        except Exception as exc:
            logger.warning("cache.get file error for %s: %s", key, exc)
            self._count("misses")
            return None

        try:
            tables_rows = self._exec(
                "SELECT table_ref FROM cache_entry_tables WHERE cache_key = ?", [key]
            ).fetchall()
            physical_tables = [r[0] for r in tables_rows]
        except Exception:
            physical_tables = []

        columns = None
        if columns_json:
            with contextlib.suppress(Exception):
                columns = json.loads(columns_json)

        self._count("hits")
        return CachedResult(
            payload=payload,
            cached_at=created_at if isinstance(created_at, datetime) else now,
            ttl_remaining_seconds=max(0, int((expires_at - now).total_seconds())),
            physical_tables=physical_tables,
            row_count=int(row_count) if row_count is not None else 0,
            columns=columns,
        )

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
        if ttl_seconds <= 0:
            return
        if len(payload) > self._max_value_bytes:
            return

        file_path = self._file_path(datasource, key)
        tmp_path = file_path + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, file_path)
        except Exception as exc:
            logger.warning("cache.set file error for %s: %s", key, exc)
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)
            return

        now = datetime.now(UTC)
        ttl = min(ttl_seconds, self._max_ttl_seconds)
        expires_at = now + timedelta(seconds=ttl)
        size_bytes = len(payload)

        try:
            with self._lock:
                self._meta.execute("BEGIN")
                self._meta.execute(
                    """
                    INSERT OR REPLACE INTO cache_entries
                        (cache_key, datasource, model_id, query_hash, dialect,
                         file_path, row_count, columns_json, size_bytes, created_at,
                         expires_at, last_hit_at, hit_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                    """,
                    [
                        key,
                        datasource,
                        model_id,
                        query_hash,
                        dialect,
                        file_path,
                        row_count,
                        columns_json,
                        size_bytes,
                        now,
                        expires_at,
                    ],
                )
                self._meta.execute("DELETE FROM cache_entry_tables WHERE cache_key = ?", [key])
                deduped = sorted(set(physical_tables))
                for table_ref in deduped:
                    self._meta.execute(
                        "INSERT INTO cache_entry_tables (cache_key, table_ref) VALUES (?, ?)",
                        [key, table_ref],
                    )
                self._meta.execute("COMMIT")
        except Exception as exc:
            logger.warning("cache.set metadata error for %s: %s", key, exc)
            with contextlib.suppress(Exception):
                self._meta.execute("ROLLBACK")
            self._unlink(file_path)

    async def delete(self, key: str) -> None:
        try:
            row = self._exec(
                "SELECT file_path FROM cache_entries WHERE cache_key = ?", [key]
            ).fetchone()
        except Exception:
            return
        if row is None:
            return
        await self._evict(key, row[0])

    async def clear(self) -> int:
        try:
            with self._lock:
                self._meta.execute("BEGIN")
                rows = self._meta.execute(
                    "DELETE FROM cache_entries RETURNING file_path"
                ).fetchall()
                self._meta.execute("DELETE FROM cache_entry_tables")
                self._meta.execute("COMMIT")
        except Exception as exc:
            logger.warning("cache.clear error: %s", exc)
            with contextlib.suppress(Exception):
                self._meta.execute("ROLLBACK")
            return 0
        for (file_path,) in rows:
            self._unlink(file_path)
        return len(rows)

    async def delete_datasource(self, datasource: str) -> int:
        try:
            with self._lock:
                self._meta.execute("BEGIN")
                rows = self._meta.execute(
                    "DELETE FROM cache_entries WHERE datasource = ? RETURNING cache_key, file_path",
                    [datasource],
                ).fetchall()
                keys = [r[0] for r in rows]
                if keys:
                    placeholders = ",".join("?" * len(keys))
                    self._meta.execute(
                        f"DELETE FROM cache_entry_tables WHERE cache_key IN ({placeholders})",
                        keys,
                    )
                self._meta.execute("COMMIT")
        except Exception as exc:
            logger.warning("cache.delete_datasource error: %s", exc)
            with contextlib.suppress(Exception):
                self._meta.execute("ROLLBACK")
            return 0
        for _, file_path in rows:
            self._unlink(file_path)
        return len(rows)

    async def invalidate_table(self, table_ref: str) -> int:
        try:
            with self._lock:
                self._meta.execute("BEGIN")
                rows = self._meta.execute(
                    """
                    DELETE FROM cache_entries
                    WHERE cache_key IN (
                        SELECT cache_key FROM cache_entry_tables WHERE table_ref = ?
                    )
                    RETURNING cache_key, file_path
                    """,
                    [table_ref],
                ).fetchall()
                keys = [r[0] for r in rows]
                if keys:
                    placeholders = ",".join("?" * len(keys))
                    self._meta.execute(
                        f"DELETE FROM cache_entry_tables WHERE cache_key IN ({placeholders})",
                        keys,
                    )
                self._meta.execute("COMMIT")
        except Exception as exc:
            logger.warning("cache.invalidate_table error: %s", exc)
            with contextlib.suppress(Exception):
                self._meta.execute("ROLLBACK")
            return 0
        for _, file_path in rows:
            self._unlink(file_path)
        if rows:
            self._bump("heartbeat_invalidations", len(rows))
        return len(rows)

    async def stats(self) -> CacheStats:
        # Fold any in-memory hits/misses into the DuckDB counters before reading.
        self._flush_counters()
        try:
            row = self._exec(
                "SELECT count(*), coalesce(sum(size_bytes), 0), min(created_at) FROM cache_entries"
            ).fetchone()
            count = int(row[0]) if row else 0
            total = int(row[1]) if row else 0
            oldest = row[2] if row else None
        except Exception:
            count, total, oldest = 0, 0, None

        try:
            counters = self._exec("SELECT name, value FROM cache_counters").fetchall()
            counter_map = dict(counters)
        except Exception:
            counter_map = {}

        try:
            tracked = self._exec(
                "SELECT count(DISTINCT table_ref) FROM cache_entry_tables"
            ).fetchone()
            tracked_count = int(tracked[0]) if tracked else 0
        except Exception:
            tracked_count = 0

        hits = int(counter_map.get("hits", 0))
        misses = int(counter_map.get("misses", 0))
        total_lookups = hits + misses
        hit_rate = round(hits / total_lookups, 3) if total_lookups else 0.0

        # Use the canonical "Z" suffix for UTC instead of Python's default
        # "+00:00", matching the format used elsewhere in the API
        # (e.g. /v1/settings.timezone.now). datetime.isoformat() doesn't
        # accept a Z suffix flag, so we substitute after the fact.
        def _utc_iso(dt: datetime) -> str:
            return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

        next_sweep = None
        if self._next_sweep_at is not None:
            next_sweep = _utc_iso(datetime.fromtimestamp(self._next_sweep_at, tz=UTC))

        return CacheStats(
            backend=self.backend_name,
            entry_count=count,
            total_size_bytes=total,
            max_size_bytes=self._max_disk_bytes,
            hit_count_total=hits,
            miss_count_total=misses,
            hit_rate=hit_rate,
            oldest_entry=(_utc_iso(oldest) if isinstance(oldest, datetime) else None),
            next_sweep_at=next_sweep,
            tracked_physical_tables=tracked_count,
            heartbeat_invalidations_total=int(counter_map.get("heartbeat_invalidations", 0)),
        )

    async def record_hit(self, key: str) -> None:
        now = datetime.now(UTC)
        try:
            self._exec(
                "UPDATE cache_entries SET last_hit_at = ?, hit_count = hit_count + 1 "
                "WHERE cache_key = ?",
                [now, key],
            )
        except Exception:
            return

    async def record_heartbeat(self, table_ref: str, observed_at: datetime) -> None:
        """Record a heartbeat for a physical table (idempotent on regression)."""
        now = datetime.now(UTC)
        try:
            row = self._exec(
                "SELECT last_heartbeat FROM table_heartbeats WHERE table_ref = ?",
                [table_ref],
            ).fetchone()
            if row is not None and isinstance(row[0], datetime) and row[0] >= observed_at:
                return  # idempotency: ignore older or equal observations
            self._exec(
                """
                INSERT INTO table_heartbeats (table_ref, last_heartbeat, received_at)
                VALUES (?, ?, ?)
                ON CONFLICT (table_ref) DO UPDATE SET
                    last_heartbeat = EXCLUDED.last_heartbeat,
                    received_at    = EXCLUDED.received_at
                """,
                [table_ref, observed_at, now],
            )
        except Exception:
            logger.debug("record_heartbeat failed", exc_info=True)

    def heartbeats_snapshot(self) -> dict[str, datetime]:
        """Return ``{table_ref: last_heartbeat}`` for TTL derivation."""
        try:
            rows = self._exec("SELECT table_ref, last_heartbeat FROM table_heartbeats").fetchall()
        except Exception:
            return {}
        out: dict[str, datetime] = {}
        for ref, ts in rows:
            if isinstance(ts, datetime):
                out[str(ref)] = ts
        return out

    async def shutdown(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(BaseException):
                await self._sweep_task
            self._sweep_task = None
        with contextlib.suppress(Exception):
            self._flush_counters()
        with contextlib.suppress(Exception), self._lock:
            self._meta.close()

    # -- maintenance --------------------------------------------------------

    async def _evict(self, key: str, file_path: str | None) -> None:
        try:
            with self._lock:
                self._meta.execute("BEGIN")
                self._meta.execute("DELETE FROM cache_entries WHERE cache_key = ?", [key])
                self._meta.execute("DELETE FROM cache_entry_tables WHERE cache_key = ?", [key])
                self._meta.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                self._meta.execute("ROLLBACK")
            return
        if file_path:
            self._unlink(file_path)

    async def sweep_once(self) -> tuple[int, int]:
        """Run a single sweep pass. Returns ``(ttl_evicted, capacity_evicted)``."""
        self._flush_counters()
        ttl_evicted = await self._sweep_ttl()
        capacity_evicted = await self._sweep_capacity()
        return ttl_evicted, capacity_evicted

    async def _sweep_ttl(self) -> int:
        now = datetime.now(UTC)
        try:
            with self._lock:
                self._meta.execute("BEGIN")
                rows = self._meta.execute(
                    "DELETE FROM cache_entries WHERE expires_at <= ? "
                    "RETURNING cache_key, file_path",
                    [now],
                ).fetchall()
                keys = [r[0] for r in rows]
                if keys:
                    placeholders = ",".join("?" * len(keys))
                    self._meta.execute(
                        f"DELETE FROM cache_entry_tables WHERE cache_key IN ({placeholders})",
                        keys,
                    )
                self._meta.execute("COMMIT")
        except Exception as exc:
            logger.warning("cache sweep TTL error: %s", exc)
            with contextlib.suppress(Exception):
                self._meta.execute("ROLLBACK")
            return 0
        for _, file_path in rows:
            self._unlink(file_path)
        return len(rows)

    async def _sweep_capacity(self) -> int:
        try:
            row = self._exec("SELECT coalesce(sum(size_bytes), 0) FROM cache_entries").fetchone()
            total = int(row[0]) if row else 0
        except Exception:
            return 0
        if total <= self._max_disk_bytes:
            return 0
        evicted = 0
        while total > self._max_disk_bytes:
            try:
                with self._lock:
                    self._meta.execute("BEGIN")
                    rows = self._meta.execute(
                        """
                        DELETE FROM cache_entries WHERE cache_key IN (
                            SELECT cache_key FROM cache_entries
                            ORDER BY last_hit_at NULLS FIRST, created_at ASC
                            LIMIT 100
                        )
                        RETURNING cache_key, file_path, size_bytes
                        """
                    ).fetchall()
                    keys = [r[0] for r in rows]
                    if keys:
                        placeholders = ",".join("?" * len(keys))
                        self._meta.execute(
                            f"DELETE FROM cache_entry_tables WHERE cache_key IN ({placeholders})",
                            keys,
                        )
                    self._meta.execute("COMMIT")
            except Exception as exc:
                logger.warning("cache sweep capacity error: %s", exc)
                with contextlib.suppress(Exception):
                    self._meta.execute("ROLLBACK")
                return evicted
            if not rows:
                break
            for _, file_path, _size in rows:
                self._unlink(file_path)
            evicted += len(rows)
            total -= sum(int(r[2] or 0) for r in rows)
        return evicted

    def start_sweep_task(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Spawn the periodic sweep on a running event loop. Idempotent."""
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        loop = loop or asyncio.get_event_loop()
        self._sweep_task = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        try:
            while True:
                self._next_sweep_at = time.time() + self._sweep_interval_seconds
                await asyncio.sleep(self._sweep_interval_seconds)
                with contextlib.suppress(Exception):
                    await self.sweep_once()
        except asyncio.CancelledError:
            return
