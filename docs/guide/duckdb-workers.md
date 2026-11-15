---
description: "Run a DuckDB data source's queries on Hardcharger workers beside OrionBelt: DuckDB servers that stream Arrow, for lake sources whose load needs more than one process."
---

# DuckDB Workers

A DuckDB data source runs its queries inside the OrionBelt process. When one
source's load needs more than that, for example many queries at once over
Parquet files or Iceberg tables, its queries can run on **Hardcharger
workers** instead: DuckDB servers that stream their results as Arrow.

Most deployments need none of this. A DuckDB source without the setting below,
and every other data source, runs as before, and nothing of the workers' code
runs for it.

## Turn it on for a source

```bash
OBSL_DATABASES=lake
DB_LAKE_TYPE=duckdb
DB_LAKE_DUCKDB_BACKEND=hardcharger
DB_LAKE_HARDCHARGER_WORKERS=hardcharger://hardcharger-1?tls=false,hardcharger://hardcharger-2?tls=false
DB_LAKE_HARDCHARGER_USER=obsl_lake
DB_LAKE_HARDCHARGER_PASSWORD=change-me
DB_LAKE_DUCKDB_CONNECTION_SQL=SET search_path = 'lake.sales'
```

- **The driver.** The `ralforion/orionbelt-semantic-layer-flight` image
  carries it. Otherwise install OrionBelt with its `hardcharger` extra,
  `pip install 'orionbelt-semantic-layer[hardcharger]'`, or build the API
  image with `--build-arg OB_EXTRA="flight-duckdb-only hardcharger"`.
- **The models do not change.** The dialect is still `duckdb`, and the SQL is
  the same. REST, the Postgres wire, Flight SQL, `EXPLAIN`, online validation
  and value search all run it on the workers.
- **Checked at startup.** A source set to `hardcharger` that lacks a setting,
  or runs without the driver installed, stops the server from starting, and the
  message names the source and the setting.

## Settings

| Setting | Default | What it sets |
|---|---|---|
| `DB_<NAME>_DUCKDB_BACKEND` | `embedded` | `hardcharger`: the source's queries run on its workers. Set per source: the unprefixed `DUCKDB_BACKEND` moves no named source, and the implicit DuckDB source cannot run on workers |
| `DB_<NAME>_HARDCHARGER_WORKERS` | | The workers, comma-separated: `hardcharger://host[:port]`. The port is 31337 unless given. TLS is on unless the address ends in `?tls=false` |
| `DB_<NAME>_HARDCHARGER_USER`, `DB_<NAME>_HARDCHARGER_PASSWORD` | | The user the workers' init SQL creates for the source |
| `DB_<NAME>_HARDCHARGER_TLS_ROOT_CERTS` | the system's | A PEM file with the CA that signed the workers' certificates |
| `DB_<NAME>_HARDCHARGER_CONNECTIONS_PER_WORKER` | `4` | Connections of one worker in use at once. Keep it within the worker's statement concurrency plus its queue |
| `DB_<NAME>_HARDCHARGER_QUERY_TIMEOUT_SECONDS` | `120` | Each statement's timeout, set on the worker |
| `DB_<NAME>_HARDCHARGER_HEALTH_INTERVAL_SECONDS` | `5` | How often each worker's health is checked |
| `DB_<NAME>_HARDCHARGER_CONNECT_TIMEOUT_SECONDS` | `10` | How long opening a connection to a worker may take |
| `DB_<NAME>_DUCKDB_CONNECTION_SQL` | | Statements each session on a worker runs first, such as `SET search_path` and `SET TimeZone`. They run like a query: with its timeout, and retried like one. A session starts in the worker's time zone (UTC in the Hardcharger image), where DuckDB in process takes the host's; a `TIMESTAMPTZ` result carries that zone |
| `DB_<NAME>_MAX_CONNECTIONS`, `DB_<NAME>_ACQUIRE_TIMEOUT_SECONDS` | `10`, `30` | As for every source: how many of its queries run at once, and how long a query waits for a connection |

Like a source's other settings, an unprefixed `HARDCHARGER_WORKERS` (and so
on) applies to each source that sets none of its own.

## What the workers set up

The workers read what their own init SQL sets up, so the settings that set up
DuckDB inside OrionBelt do not apply to a source on the workers. They are
refused rather than ignored:

| Setting | On the workers instead |
|---|---|
| `DB_<NAME>_DUCKDB_INIT_SQL` | The workers' init SQL: secrets, `ATTACH` of a catalog |
| `DB_<NAME>_DUCKDB_ALLOWED_PATHS` | The location grants of the source's user |
| `DB_<NAME>_DUCKDB_MEMORY_LIMIT` | The workers' `HARDCHARGER_MEMORY_LIMIT` |
| `DB_<NAME>_DUCKDB_THREADS` | The workers' CPU limit |
| `DB_<NAME>_DUCKDB_SPILL_DIR`, `DB_<NAME>_DUCKDB_SPILL_MAX_MB` | The workers' own temporary directory |
| `DB_<NAME>_DUCKDB_DATABASE` | The workers' database; their init SQL attaches what they read |

The source's user reads what its grants allow, and nothing else:

```sql
CREATE ROLE IF NOT EXISTS lake_reader;
GRANT READ FILESYSTEM ON LOCATION '/data/' TO lake_reader;
GRANT READ OBJECT STORAGE ON LOCATION 's3://warehouse/' TO lake_reader;
GRANT SELECT ON CATALOG lake TO lake_reader;
CREATE USER IF NOT EXISTS obsl_lake WITH PASSWORD 'change-me';
GRANT lake_reader TO obsl_lake;
```

The user cannot create secrets, attach a database or change the workers'
settings. A read outside its grants is refused, and the error names the
privilege it needs.

## How queries run

| When | What happens |
|---|---|
| Every query | It goes to the healthy worker with the fewest connections in use, the workers in turn on a tie, and keeps that worker's connection until its result is read |
| A worker cannot be reached, or opening a connection to it does not come back in time | The worker takes no more queries, and the query runs on another |
| The worker no longer has the session (it restarted, or ended a session left idle) | A new session runs `DUCKDB_CONNECTION_SQL`, then the query |
| A worker's statement queue is full (HTTP 429) | The query runs again, on another worker if one has a connection free |
| A worker is shutting down (it refuses new statements, HTTP 503) | It takes no more queries, the query runs on another, and the queries running on it finish |
| A worker's health check answers, but not `serving` | It takes no queries until it answers `serving` again. The queries running on it carry on |
| A worker's health check does not answer twice in a row | The queries running on it end, too |
| A statement goes unanswered 10 s past its timeout | It ends, and its worker takes no queries until its health check passes. A worker that dies without closing its connections sends nothing more, so this is how such a query ends |
| Any other failure | It is the query's own, and the query does not run again |
| No worker answers | The query is refused with HTTP 503, `[WORKERS_UNAVAILABLE]` |
| Every connection stayed in use for the whole wait | HTTP 503, `[DATA_SOURCE_BUSY]` |

The pools of the sources on workers start with the server, so each worker's
health is known before the first query.

## When object storage or a catalog stops answering

DuckDB waits 30 s for each HTTP request and tries it 3 more times, so a query
on a worker waits about 2 minutes before it fails. Shorter limits in the
workers' init SQL, before the `ATTACH`, fail it sooner:

```sql
SET GLOBAL http_timeout = 10;
SET GLOBAL http_retries = 2;
```

| Measured, object store stopped | Default | With the limits above |
|---|---|---|
| An Iceberg query on a worker | Failed after 120 s | Failed after about 30 s |
| The next query, the store back | Read | Read |

The catalog was what hung: asked for a table, it waited on the object store
too. The limit is per request, so it has to leave room for the largest file
read a query makes. It has to be `SET GLOBAL`: a plain `SET` lasts for the init
SQL's own connection only.

## Metrics

OrionBelt serves Prometheus metrics when `METRICS_ENABLED=true`, on a port of
its own, not the API's:

| Setting | Default | What it sets |
|---|---|---|
| `METRICS_ENABLED` | `false` | Serve `/metrics` |
| `METRICS_HOST` | `0.0.0.0` | The address it listens on |
| `METRICS_PORT` | `9090` | Its port |

For the pools, read at each scrape, labelled by `source` and `worker` (the
`host:port` the source lists):

| Metric | Type | What it counts |
|---|---|---|
| `obsl_duckdb_worker_up` | gauge | 1 while the worker takes queries, 0 while the pool keeps it out |
| `obsl_duckdb_worker_connections` | gauge | Connections by `state`: `in_use` (held by a query), `idle`, `opening` (a connect given up on that has not come back) |
| `obsl_duckdb_worker_statements_total` | counter | Statements the worker ran, those that failed or that the pool ended included. One it refused before running counts as a retry |
| `obsl_duckdb_worker_retries_total` | counter | Queries run again because the worker did not start them, by `reason`: `unreachable`, `connect_timeout`, `session_not_found`, `rate_limited`, `server_draining` |
| `obsl_duckdb_worker_statements_ended_total` | counter | Statements the pool ended, by `reason`: `deadline` or `unresponsive` |

The process's own metrics come with them: CPU, memory and open files where the
system reports them, the Python version, and garbage collection. Each worker
serves Hardcharger's own metrics too, on its port 9091
(`HARDCHARGER_ENABLE_METRICS`), with its queue and statements. A scrape
configuration for both:

```yaml
scrape_configs:
  - job_name: obsl
    static_configs:
      - targets: ["obsl:9090"]
  - job_name: hardcharger
    static_configs:
      - targets: ["hardcharger-1:9091", "hardcharger-2:9091"]
```

## In Docker Compose

The repository's `deploy/compose/` runs OrionBelt and two workers in one
Compose project: `compose.pool.yml` adds the workers and builds OrionBelt with
the driver, and `obsl.env.example` holds the settings above in a block to
uncomment. Its
[README](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/deploy/compose)
has every setting of the workers, and a worker on another machine over TLS.

## Limitations

- **One query, one worker.** A query is not split across workers. More
  workers serve more queries at once.
- **Federation.** A federated query's legs on a source on the workers run
  there; the step that combines the legs runs inside OrionBelt, as before.
- **A worker needs its catalog to start.** A worker whose init SQL cannot
  attach a catalog does not start. Docker restarts it until the catalog
  answers.
- **Object storage or a catalog that does not answer.** A query waits until
  DuckDB's HTTP limits give up, about 2 minutes by default; see
  [above](#when-object-storage-or-a-catalog-stops-answering).
