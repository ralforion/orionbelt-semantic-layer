# Dremio ↔ OBSL pgwire compatibility tests (Stage 1)

This suite spins up Dremio OSS and registers OBSL's pgwire surface as a
**Postgres source** in Dremio, then exercises catalog reflection and a
real semantic query through Dremio's JDBC pushdown.

It is the highest-value real-world stress test of the v2.5.0 pgwire compat
work: Dremio's Postgres connector probes `pg_catalog` aggressively (much
like Tableau / pgjdbc) before any user SQL runs.

## Layout

| File | Purpose |
|---|---|
| `docker-compose.yml` | OBSL (pgwire on :5432) + Dremio OSS (REST :9047, Flight :31010) on a shared bridge network |
| `conftest.py` | Session-scoped fixture: waits for Dremio, bootstraps the admin user, registers the OBSL Postgres source via `/api/v3/catalog` |
| `test_dremio_postgres_source.py` | Three asserts: source registers, `INFORMATION_SCHEMA.TABLES` reflects, semantic SELECT round-trips |
| `run.sh` | One-shot runner — build, up, pytest, down |

## Why opt-in

The Dremio OSS image is ~2 GB and takes 30–60 s to become healthy. The
suite is therefore gated behind the `dremio` pytest marker and is never
collected by the default `uv run pytest` invocation.

## How to run

One-shot (recommended):

```bash
tests/integration/dremio/run.sh
```

Manual, if you want to keep the stack up between iterations:

```bash
docker compose -f tests/integration/dremio/docker-compose.yml up -d --build
uv run pytest -m dremio tests/integration/dremio/
docker compose -f tests/integration/dremio/docker-compose.yml down -v
```

Host port map:

| Service | Container port | Host port |
|---|---|---|
| OBSL REST API | 8080 | 18080 |
| OBSL pgwire | 5432 | 15432 |
| Dremio REST/UI | 9047 | 19047 |
| Dremio Flight | 31010 | 31010 |

Dremio admin during the test run: `obsl_admin` / `obsl_admin_pw_123!`
(visit `http://localhost:19047` if you need to poke around manually).

## Environment overrides

The fixtures honour the following env vars for CI or non-default ports:

| Variable | Default |
|---|---|
| `DREMIO_REST_URL` | `http://localhost:19047` |
| `OBSL_PGWIRE_HOST` | `obsl` (the docker network alias) |
| `OBSL_PGWIRE_PORT` | `5432` |
| `OBSL_MODEL_NAME` | `commerce` (the model name served by the `demo/dremio/` stack; `run.sh` overrides it to `orionbelt_1_commerce` for the dedicated test stack) |

## What this suite does NOT cover

This is **Stage 1**. It only validates that Dremio can use OBSL as a
Postgres source. The "full circle" picture (Dremio executes the SQL
OBSL emits via its `dremio` dialect against real lakehouse data) is
Stage 2 and would add an Iceberg/Nessie dataset inside Dremio plus an
OBSL model that points at it.
