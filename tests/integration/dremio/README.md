# Dremio ↔ OBSL integration tests

This suite spins up Dremio OSS beside OBSL and exercises the connection in
both directions:

- **Dremio → OBSL** (Stage 1): OBSL's pgwire surface registered as a
  **Postgres source** in Dremio, driving catalog reflection and a real
  semantic query through Dremio's JDBC pushdown. The highest-value
  real-world stress test of the v2.5.0 pgwire compat work: Dremio's Postgres
  connector probes `pg_catalog` aggressively (much like Tableau / pgjdbc)
  before any user SQL runs.
- **OBSL → Dremio** (Stage 2): OBSL compiles to its `dremio` dialect and
  executes back against the same container through the `ob-dremio` driver,
  which speaks Arrow Flight SQL over ADBC.
- **`ob-dremio` on its own**: the driver against Dremio's Flight SQL port,
  with nothing of OBSL in between.

## Layout

| File | Purpose |
|---|---|
| `docker-compose.yml` | OBSL (pgwire on :5432) + Dremio OSS (REST :9047, legacy :31010, Flight SQL :32010) on a shared bridge network |
| `conftest.py` | Session-scoped fixtures: `dremio_admin_token` waits for Dremio and bootstraps the admin user; `dremio_session` adds the OBSL Postgres source via `/api/v3/catalog` |
| `test_dremio_postgres_source.py` | Stage 1 — source registers, `INFORMATION_SCHEMA.TABLES` reflects, semantic SELECT round-trips |
| `test_dremio_full_circle.py` | Stage 2 — Dremio → OBSL pgwire → `ob-dremio` → Dremio, on the `dremio_info_schema` model |
| `test_dremio_function_catalog.py`, `test_dremio_measure_sweep.py` | Stage 2 breadth — functions and measures compiled to Dremio SQL and executed |
| `test_dremio_adbc_driver.py` | The `ob-dremio` driver straight against Flight SQL :32010: PEP 249 surface, Arrow types, parameter binding |
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
| Dremio legacy Flight | 31010 | 31010 |
| Dremio Flight SQL | 32010 | 32010 |

Dremio admin during the test run: `obsl_admin` / `obsl_admin_pw_123!`
(visit `http://localhost:19047` if you need to poke around manually).

## Environment overrides

The fixtures honour the following env vars for CI or non-default ports:

| Variable | Default |
|---|---|
| `DREMIO_REST_URL` | `http://localhost:19047` |
| `OBSL_PGWIRE_HOST` | `obsl` (the docker network alias) |
| `OBSL_PGWIRE_PORT` | `5432` |
| `OBSL_MODEL_NAME` | `orionbelt_1_commerce` (what the compose stack in this directory serves). Export `commerce` to point the suite at the separate `demo/dremio/` stack instead |
| `DREMIO_FLIGHT_HOST` | `localhost` (the `ob-dremio` suite connects from the host) |
| `DREMIO_FLIGHT_PORT` | `32010` |

## What this suite does NOT cover

Stage 2 runs against Dremio's own `INFORMATION_SCHEMA`, which is always
present and needs no setup. It does not run against **real lakehouse
data**: an Iceberg/Nessie dataset inside Dremio, with an OBSL model
pointing at it, would exercise pushdown and partitioning that system
tables cannot.
