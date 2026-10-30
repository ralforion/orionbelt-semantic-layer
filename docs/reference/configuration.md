---
description: "Every OrionBelt environment variable and .env setting: database connections, the surfaces to enable, authentication, admin-curated mode, caching and logging."
---

# Configuration

Configuration is via environment variables or a `.env` file. See `.env.template` for all options.

## Environment Variables

| Variable                   | Default     | Description                               |
| -------------------------- | ----------- | ----------------------------------------- |
| `LOG_LEVEL`                | `INFO`      | Logging level                             |
| `LOG_FORMAT`               | `console`   | `console` (pretty) or `json` (structured) |
| `API_SERVER_HOST`          | `localhost` | REST API bind host                        |
| `API_SERVER_PORT`          | `8000`      | REST API bind port                        |
| `PORT`                     | —           | Override port (Cloud Run sets this)       |
| `DISABLE_SESSION_LIST`     | `false`     | Disable `GET /sessions` endpoint          |
| `SESSION_TTL_SECONDS`      | `1800`      | Session inactivity timeout (30 min)       |
| `SESSION_MAX_AGE_SECONDS`  | `86400`     | Absolute max session lifetime (24 h)      |
| `SESSION_CLEANUP_INTERVAL` | `60`        | Cleanup sweep interval (seconds)          |
| `MAX_SESSIONS`             | `500`       | Global concurrent session cap (429 when full) |
| `MAX_MODELS_PER_SESSION`   | `10`        | Max models a single session may hold      |
| `SESSION_RATE_LIMIT`       | `10`        | Max `POST /sessions` per IP per minute    |
| `TRUSTED_PROXY_COUNT`      | `0`         | Number of trusted reverse proxies (for X-Forwarded-For) |
| `MODEL_FILES`              | —           | Comma-separated OBML YAML paths for admin-curated mode. Each file is loaded into its own named protected session (addressing name = OBML `name:` field or filename stem). A single path is fine. |
| `API_BASE_URL`             | —           | API URL for standalone UI                 |
| `ROOT_PATH`                | —           | ASGI root path for UI behind LB           |
| `FLIGHT_ENABLED`           | `false`     | Enable Flight SQL + query execution       |
| `FLIGHT_PORT`              | `8815`      | Arrow Flight SQL gRPC port                |
| `FLIGHT_AUTH_MODE`         | `none`      | `none` or `token`                         |
| `FLIGHT_API_TOKEN`         | —           | Static token (when auth mode = token)     |
| `PGWIRE_ENABLED`           | `false`     | Enable PostgreSQL wire-protocol surface (v2.5.0+) — connect any Postgres client (Tableau, DBeaver, Superset, Power BI, `psql`, Dremio's Postgres source) |
| `PGWIRE_HOST`              | `0.0.0.0`   | pgwire bind address                       |
| `PGWIRE_PORT`              | `5432`      | pgwire TCP port                           |
| `PGWIRE_AUTH_MODE`         | `trust`     | `trust` today; `password` / `scram-sha-256` planned alongside the unified-auth subsystem |
| `PGWIRE_MAX_CONNECTIONS`   | `64`        | Concurrent connection cap                 |
| `PGWIRE_QUERY_TIMEOUT_SECONDS` | `60`    | Per-query wall-clock timeout              |
| `API_TLS_CERT`             | —           | PEM certificate; with `API_TLS_KEY` the REST API serves HTTPS (v2.29.0+) |
| `API_TLS_KEY`              | —           | PEM private key. Both or neither: one alone refuses to start rather than serving plaintext |
| `API_TLS_CLIENT_CA`        | —           | PEM CA for client certificates; turns on mutual TLS (needs cert + key) |
| `PGWIRE_TLS_CERT`          | —           | PEM certificate; with the key, pgwire answers `S` to an `SSLRequest` and upgrades the socket (v2.28.0+) |
| `PGWIRE_TLS_KEY`           | —           | PEM private key. Both or neither                  |
| `PGWIRE_TLS_CLIENT_CA`     | —           | PEM CA for client certificates; turns on mutual TLS |
| `FLIGHT_TLS_CERT`          | —           | PEM certificate; with the key, Flight serves `grpc+tls` instead of `grpc` (v2.28.0+) |
| `FLIGHT_TLS_KEY`           | —           | PEM private key. Both or neither                  |
| `FLIGHT_TLS_CLIENT_CA`     | —           | PEM CA for client certificates; turns on mutual TLS |
| `DB_VENDOR`                | `duckdb`    | Database vendor for query execution       |
| `DB_POOL_SIZE`             | `5`         | Idle connections kept per data source     |
| `DB_MAX_CONNECTIONS`       | `10`        | Connections of one data source in use at once, on every surface; `0` for no limit. Queries past it wait in order of arrival |
| `DB_ACQUIRE_TIMEOUT_SECONDS` | `30`      | How long a query waits for a connection before `DATA_SOURCE_BUSY` (503) |
| `DB_KEY_INJECTION_MAX_KEYS` | by engine  | Most keys a federated query lists in a fact leg ([key injection](../guide/data-sources.md#key-injection)); `0` turns it off. Default 10,000 on Postgres, MySQL and ClickHouse, 1,000 on DuckDB, 0 elsewhere |
| `OBSL_DATABASES`           | —           | Comma-separated names of [named data sources](../guide/data-sources.md); each reads `DB_<NAME>_TYPE`, `DB_<NAME>_DESCRIPTION`, `DB_<NAME>_POOL_SIZE`, `DB_<NAME>_MAX_CONNECTIONS`, `DB_<NAME>_ACQUIRE_TIMEOUT_SECONDS`, `DB_<NAME>_KEY_INJECTION_MAX_KEYS` and its dialect's variables behind the `DB_<NAME>_` prefix. A model picks one with `settings.defaultDataSource` |
| `FEDERATION_MAX_CONCURRENT` | `4`       | [Queries across data sources](../guide/data-sources.md#running-a-federated-query) running at once per process; the others wait in order of arrival |
| `FEDERATION_QUEUE_TIMEOUT_SECONDS` | `30` | How long a federated query waits for a slot before `FEDERATION_BUSY` (503) |
| `FEDERATION_LEG_THREADS`   | `16`        | Legs of federated queries fetched at once per process |
| `FEDERATION_MAX_LEG_ROWS`  | `1000000`   | Most rows one leg may return; a larger leg is refused with `FEDERATION_LEG_TOO_LARGE` |
| `FEDERATION_COMBINE_THREADS` | `2`       | DuckDB threads of each federated query's combine step |
| `FEDERATION_COMBINE_MEMORY_LIMIT` | `512MB` | DuckDB `memory_limit` of each combine step |
| `FEDERATION_SPILL_DIR`     | —           | Directory on real disk where large federated legs are [spilled](../guide/data-sources.md#running-a-federated-query); unset keeps every leg in memory. Not an in-memory filesystem such as Cloud Run's default |
| `FEDERATION_SPILL_THRESHOLD_MB` | `64`   | A leg past this size goes to a file instead of memory |
| `FEDERATION_SPILL_MAX_MB`  | `4096`      | Most one federated query may spill; past it, `FEDERATION_SPILL_TOO_LARGE` |
| `FEDERATION_SPILL_BUDGET_MB` | `16384`   | Most the federated queries running at once may spill together; past it, `FEDERATION_SPILL_FULL` (503) |
| `VALUE_SEARCH_ENABLED`     | `true`      | Enable `POST /v1/values` (dimension-value search; also needs query execution) |
| `VALUE_SEARCH_MAX_LIMIT`   | `200`       | Most values one value search returns      |
| `VALUE_SEARCH_FUZZY_DOMAIN_MAX` | `10000` | Largest number of values a dimension may have for the fuzzy (misspelling) stage |
| `VALUE_SEARCH_TIMEOUT_MS`  | `30000`     | Wall-clock limit of one value search      |
| `COMPILE_CACHE_MAX_ENTRIES` | `2048`    | Compiled queries kept in memory, shared by REST, pgwire and Flight (about 7.5 MB at 2048); `0` disables the compilation cache (see [endpoints](../api/endpoints.md#compilation-cache)) |
| `COMPILE_CACHE_MAX_BYTES`  | `67108864`  | Estimated memory cap of the compilation cache (64 MB) |
| `COMPILE_CACHE_MAX_ENTRY_BYTES` | `1048576` | A compiled query larger than this (1 MB) is not cached |

## Admin-Curated Mode

When `MODEL_FILES` is set to one or more OBML YAML paths, the server starts in **admin-curated mode**:

- Every model file is validated at startup (the server refuses to start if any is invalid)
- Each model loads into its own *named protected session* — the addressing name comes from the OBML top-level `name:` field, falling back to the filename stem
- Model upload (`POST /v1/sessions/{id}/models`) and removal (`DELETE /v1/sessions/{id}/models/{id}`) return **403 Forbidden** while the flag is on
- BI tools select the model via the Flight `database` header, the pgwire `database=` URL parameter, or by addressing the named REST routes (`POST /v1/sessions/<model_name>/query/semantic-ql`)
- All other endpoints (sessions, query, validate, diagram, etc.) work normally

```bash
# One model
MODEL_FILES=./examples/orionbelt_1_commerce.yaml uv run orionbelt-api

# Multiple models, comma-separated
MODEL_FILES=./models/sales.yaml,./models/finance.yaml uv run orionbelt-api
```

This is the recommended mode for production deployments, BI tool integrations, and AI agents.
