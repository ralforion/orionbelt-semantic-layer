---
description: "OrionBelt's Prometheus metrics: REST requests, queries by surface and data source, the caches and sessions, and the DuckDB worker pools, off unless METRICS_ENABLED."
---

# Metrics

OrionBelt serves Prometheus metrics when `METRICS_ENABLED=true`. They answer
at `/metrics` on a port of their own, not the API's, so they stay off a public
API unless that port is published.

| Setting | Default | What it sets |
|---|---|---|
| `METRICS_ENABLED` | `false` | Serve `/metrics` |
| `METRICS_HOST` | `0.0.0.0` | The address it listens on |
| `METRICS_PORT` | `9090` | Its port |

Off, nothing of it runs: no request or query is counted, and the Prometheus
client library is not loaded.

## Requests and queries

| Metric | Type | Labels | What it counts |
|---|---|---|---|
| `obsl_http_requests_total` | counter | `method`, `route`, `status` | REST API requests |
| `obsl_http_request_duration_seconds` | histogram | `method`, `route` | How long they took |
| `obsl_queries_total` | counter | `surface`, `dialect`, `source`, `outcome` | Queries run on a database |
| `obsl_query_duration_seconds` | histogram | `surface`, `dialect`, `source` | How long they took, results fetched |

- **`method`** is the request's method. A method HTTP does not define is
  `other`, since a client can send any word as one.
- **`route`** is the route's template, such as
  `/v1/sessions/{session_id}/query/execute`, so one series covers every
  session. A path no route has is `unmatched`.
- **`surface`** is where the query came in: `rest`, `pgwire` or `flight`.
  `other` is everything else, such as the command line.
- **`source`** is the data source's name, or the dialect for a source not
  named.
- **`outcome`** says how the query ended:
  - `ok`;
  - `error`: the database refused it;
  - `unavailable`: it could not run, for example no connection was free or the
    source is not set up.
- **What counts as a query.** A result served from the result cache runs none.
  A federated query counts each of its legs, on the leg's own source.

## Caches and sessions

Read at each scrape.

| Metric | Type | What it says |
|---|---|---|
| `obsl_build_info` | gauge | 1, with the running `version` |
| `obsl_sessions` | gauge | Sessions open |
| `obsl_result_cache_backend` | gauge | 1, with the result cache's `backend` |
| `obsl_result_cache_entries` | gauge | Results cached |
| `obsl_result_cache_bytes` | gauge | Their size |
| `obsl_result_cache_lookups_total` | counter | Lookups, by `result`: `hit`, `miss` |
| `obsl_compilation_cache_entries` | gauge | Compiled queries kept |
| `obsl_compilation_cache_bytes` | gauge | Their estimated size |
| `obsl_compilation_cache_lookups_total` | counter | Lookups, by `result`: `hit`, `miss`, `bypass` (not cacheable) |
| `obsl_compilation_cache_evictions_total` | counter | Compiled queries evicted to stay within the limits |

The result cache's numbers are the ones `GET /v1/cache/stats` shows. A scrape
reads them on a thread of its own, not the API's, and does not wait for the
queries using the cache. A scrape the cache cannot answer leaves them out.

## DuckDB worker pools

For each [DuckDB source on Hardcharger workers](../guide/duckdb-workers.md) and
each of its workers, labelled `source` and `worker` (the `host:port` the source
lists). Read at each scrape.

| Metric | Type | What it says |
|---|---|---|
| `obsl_duckdb_worker_up` | gauge | 1 while the worker takes queries, 0 while the pool keeps it out |
| `obsl_duckdb_worker_connections` | gauge | Connections by `state`: `in_use` (held by a query), `idle`, `opening` (a connect given up on that has not come back) |
| `obsl_duckdb_worker_statements_total` | counter | Statements the worker ran, a session's setup (`DUCKDB_CONNECTION_SQL`) as one, those that failed or that the pool ended included. One it refused before running counts as a retry |
| `obsl_duckdb_worker_retries_total` | counter | Queries run again because the worker did not start them, by `reason`: `unreachable`, `connect_timeout`, `session_not_found`, `rate_limited`, `server_draining` |
| `obsl_duckdb_worker_statements_ended_total` | counter | Statements the pool ended, by `reason`: `deadline` or `unresponsive` |

Each worker also serves Hardcharger's own metrics, on its port 9091
(`HARDCHARGER_ENABLE_METRICS`), with its statement queue.

## The process

Where the system reports them: CPU, memory and open files
(`process_*`), the Python version (`python_info`), and garbage collection
(`python_gc_*`).

## Scraping

```yaml
scrape_configs:
  - job_name: obsl
    static_configs:
      - targets: ["obsl:9090"]
  - job_name: hardcharger
    static_configs:
      - targets: ["hardcharger-1:9091", "hardcharger-2:9091"]
```

In the repository's Docker Compose setup (`deploy/compose/`), both answer on
the project network only. Publish a port to scrape them from the host.
