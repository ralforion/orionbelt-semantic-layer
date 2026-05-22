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
| `MODEL_FILE`               | —           | Path to OBML YAML for single-model mode   |
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
| `DB_VENDOR`                | `duckdb`    | Database vendor for query execution       |

## Single-Model Mode

When `MODEL_FILE` is set to a path to an OBML YAML file, the server starts in **single-model mode**:

- The model file is validated at startup (the server refuses to start if it's invalid)
- Every new session is automatically pre-loaded with the configured model
- Model upload (`POST /v1/sessions/{id}/models`) and removal (`DELETE /v1/sessions/{id}/models/{id}`) return **403 Forbidden**
- All other endpoints (sessions, query, validate, diagram, etc.) work normally

```bash
# Start in single-model mode
MODEL_FILE=./examples/sem-layer.obml.yml uv run orionbelt-api
```

This is the recommended mode for production deployments and AI integrations.
