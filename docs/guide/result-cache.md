# Result cache based on freshness inheritance

OrionBelt's result cache is **based on freshness inheritance**: instead of asking callers to pick a TTL, the cache derives one from the refresh contracts of the **physical source tables** a query touched. A heartbeat from the warehouse invalidates every cached query that depends on that table, regardless of how many semantic facets the model split it into.

This page explains how it works and how to enable it. For the underlying design, see `design/PLAN_freshness_driven_cache.md`.

!!! note "Not 'semantic caching'"
    The phrase "semantic cache" in AI/LLM contexts means embedding-based similarity matching of natural-language queries — a completely unrelated concept. OBSL uses a **cache based on freshness inheritance** or **source-aware result cache**.

## Why source-level

Most semantic layers attach freshness to the abstraction (a cube, an Explore, a saved query). When two cubes read the same physical table, each declares its own TTL — and they can drift. OBSL flips that: the contract lives on the `dataObject` that maps to the table, declared once. Two `dataObject` entries on the same table inherit the same contract automatically. One ETL heartbeat to the table invalidates every dependent cached query in one stroke.

## Default behavior

The cache is **off by default**: `CACHE_BACKEND=noop`. Every `get` misses, every `set` is a no-op. No new dependencies, no behavioral change for users who don't enable it.

## Enabling the cache

```bash
CACHE_BACKEND=file
CACHE_DIR=/var/lib/orionbelt/cache
HEARTBEAT_AUTH_TOKEN=<random-secret>
```

`CACHE_DIR` must be writable. For the cache to survive container restarts, mount it on persistent storage.

For multi-replica deployments, keep `CACHE_BACKEND=noop` until a Redis backend lands — the file cache is per-replica, and heartbeat invalidations only fire on the receiving replica.

## TTL composition

For a query that touches physical tables `{T1, …, Tn}`:

1. Look up each table's :doc:`refresh contract <freshness-contracts>`.
2. For each non-static table, compute its *contribution* (seconds until next refresh, or `max_staleness - elapsed_since_last_heartbeat`).
3. The effective TTL is the **minimum** contribution.
4. If any touched table has no declared contract, the query is **not cached** by default (`CACHE_UNKNOWN_FRESHNESS_POLICY=no_cache`). Operators who know their warehouse refresh patterns can set the policy to `default_ttl` and `CACHE_UNKNOWN_FRESHNESS_DEFAULT_TTL` to opt in.
5. Below `CACHE_MIN_TTL_SECONDS` (default 5s), the entry is not cached — heartbeat-mode tables with very short `max_staleness` would otherwise thrash.

A query whose every touched table has `mode: static` is cached for `CACHE_MAX_TTL_SECONDS` (default 24h).

## Heartbeats

When ETL refreshes a table, ping the heartbeat endpoint:

```bash
curl -X POST https://your-deployment/v1/heartbeat \
     -H "Authorization: Bearer $HEARTBEAT_AUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"database": "WAREHOUSE", "schema": "PUBLIC", "table": "ORDERS"}'
```

The body identifies a **physical** table by `database.schema.table`. There's no `dataObject` reference — ETL knows what it just refreshed; OBSL maps that to every cached query that touched the table, across every dataObject and every session.

The endpoint requires a bearer token (`HEARTBEAT_AUTH_TOKEN`). When the env var is unset, the route returns 404.

## Per-query response fields

Every `query/execute` JSON response gains:

| Field | Description |
|---|---|
| `cached` | Whether this result came from the cache. |
| `cached_at` | ISO 8601 timestamp the cached result was first computed. |
| `ttl_seconds` | Effective TTL applied to this entry. |
| `ttl_source` | `freshness_derived`, `caller_capped`, `default_unknown`, `no_cache`, or `no_cache:<reason>`. |
| `ttl_limiting_table` | The physical table whose contract drove the TTL. |
| `physical_tables` | Deduplicated `database.schema.code` strings the query touched. |

On cache hits, `execution_time_ms` reports the wall-clock time spent reading + decoding the cached entry — *not* the original database run time. The original DB timing is preserved on disk alongside the cached payload; combine `cached: true` with `execution_time_ms` to distinguish "fresh from warehouse" vs "served from cache" durations.

## UI controls

The Gradio UI exposes a **Cache Stats** panel in the Settings tab next to the API Settings YAML. It auto-loads when the tab opens and provides three buttons:

- **Refresh Cache Stats** — re-fetches `GET /v1/cache/stats` so you see current counters.
- **Sweep Cache now** — calls `POST /v1/cache/sweep` then refreshes stats. Useful when you want to reclaim disk from expired entries before the next periodic sweep (default 1 day).
- **Clear Cache** — calls `POST /v1/cache/clear` then refreshes stats. Drops all entries; counters are preserved.

The Query Results tab also annotates each execution with `(cache)` or `(database)` next to `execution_time_ms` so you can see at a glance which path served the result.

## Cache stats

```
GET /v1/cache/stats
```

Always responds. With `CACHE_BACKEND=noop` the response shows `backend: "noop"` and zero counters.

```json
{
  "backend": "file",
  "entry_count": 1247,
  "total_size_bytes": 234567890,
  "max_size_bytes": 5368709120,
  "hit_count_total": 9821,
  "miss_count_total": 4203,
  "hit_rate": 0.700,
  "oldest_entry": "2026-04-15T12:30:00Z",
  "next_sweep_at": "2026-04-15T12:45:00Z",
  "tracked_physical_tables": 8,
  "heartbeat_invalidations_total": 142
}
```

## Manual sweep

```
POST /v1/cache/sweep
```

Triggers one TTL + capacity eviction pass on demand — equivalent to a single tick of the periodic sweeper. Returns the number of entries evicted by each policy.

```json
{
  "backend": "file",
  "ttl_evicted": 17,
  "capacity_evicted": 0
}
```

With `CACHE_BACKEND=noop` returns zero counts.

## Clear cache

```
POST /v1/cache/clear
```

Drops every cache entry regardless of TTL or freshness contract — useful from the UI Settings panel when you want to start fresh. Counters (hits, misses, heartbeat invalidations) are preserved as historical telemetry.

```json
{
  "backend": "file",
  "entries_cleared": 1247
}
```

## What's cached

- Only canonical JSON `query/execute` responses with `format_values=false` are cached. TSV and locale-formatted JSON are skipped — caching them would require keying on locale/format.
- `query/sql` and `query/plan` are not cached (they don't execute against the warehouse).
- Results larger than `CACHE_MAX_VALUE_BYTES` (default 10 MB) skip the cache.

## Storage

The file backend uses two layers:

- **DuckDB** (`{CACHE_DIR}/meta.duckdb`) for the control plane: cache entries, dependency tracking, heartbeats, sweep queries.
- **Arrow IPC files** (`{CACHE_DIR}/results/…`), gzip-compressed, holding **only the row data** (type-precise, no response envelope baked in). Response metadata (sql, explain, timing, cache status) is rebuilt fresh per request from the compile result, so a cache hit reports correct per-request timing on every surface. A `format=arrow` hit ships the stored data blob verbatim behind a freshly-assembled JSON envelope, so no re-encoding of the data is needed. A single entry is shared across REST, pgwire, and Flight (keyed on the data source).

Capacity eviction is LRU (`last_hit_at NULLS FIRST, created_at ASC`); TTL eviction is lazy on read plus a periodic sweep every `CACHE_SWEEP_INTERVAL_SECONDS` (default 1 day). Lazy TTL on read keeps user-facing freshness correct, so the sweeper only matters for reclaiming disk from entries that expire without being read again.

## Cache lifecycle across restarts

The persisted cache state (`meta.duckdb` + `results/`) is **wiped on every server startup**. The reason is structural: `model_id` is generated as a fresh UUID on every model load, so any cache entries from a previous process run reference model_ids that no longer exist — they're orphans by construction. Starting empty avoids accumulating dead state between restarts.

If your deployment restarts frequently and you'd rather keep warm cache across restarts, the cache key would need to switch from `model_id` to a content hash. Not done in v1; revisit if real demand emerges.

Sibling files in `CACHE_DIR` (anything not under `meta.duckdb*` or `results/`) are not touched.

## Failure semantics

The cache **fails closed**: any error (DuckDB failure, missing file, decode error) degrades to a cache miss and the query is executed normally. Cached results never produce wrong data — at worst they're skipped.
