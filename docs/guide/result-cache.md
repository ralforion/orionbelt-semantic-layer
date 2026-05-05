# Freshness-driven result cache

OrionBelt's result cache is **freshness-driven**: instead of asking callers to pick a TTL, the cache derives one from the refresh contracts of the **physical source tables** a query touched. A heartbeat from the warehouse invalidates every cached query that depends on that table, regardless of how many semantic facets the model split it into.

This page explains how it works and how to enable it. For the underlying design, see `design/PLAN_freshness_driven_cache.md`.

!!! note "Not 'semantic caching'"
    The phrase "semantic cache" in AI/LLM contexts means embedding-based similarity matching of natural-language queries — a completely unrelated concept. OBSL uses **freshness-driven cache** or **source-aware result cache**.

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

## What's cached

- Only canonical JSON `query/execute` responses with `format_values=false` are cached. TSV and locale-formatted JSON are skipped — caching them would require keying on locale/format.
- `query/sql` and `query/plan` are not cached (they don't execute against the warehouse).
- Results larger than `CACHE_MAX_VALUE_BYTES` (default 10 MB) skip the cache.

## Storage

The file backend uses two layers:

- **DuckDB** (`{CACHE_DIR}/meta.duckdb`) for the control plane: cache entries, dependency tracking, heartbeats, sweep queries.
- **Parquet files** (`{CACHE_DIR}/results/…`) for the actual result payloads. Self-describing, type-precise, inspectable with the DuckDB CLI.

Capacity eviction is LRU (`last_hit_at NULLS FIRST, created_at ASC`); TTL eviction is lazy on read plus a periodic sweep every `CACHE_SWEEP_INTERVAL_SECONDS` (default 15 minutes).

## Failure semantics

The cache **fails closed**: any error (DuckDB failure, missing file, decode error) degrades to a cache miss and the query is executed normally. Cached results never produce wrong data — at worst they're skipped.
