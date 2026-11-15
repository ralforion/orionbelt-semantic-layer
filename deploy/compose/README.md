# OrionBelt Semantic Layer with Docker Compose

Run OrionBelt Semantic Layer (OBSL) with Docker Compose: alone, with a local
lake for development, or with DuckDB workers beside it.

| Setup | In `.env` | What runs |
|---|---|---|
| OBSL alone | `COMPOSE_FILE=compose.yml` | OBSL, for every database it supports. DuckDB runs in process, files and Iceberg tables included, and so does federation |
| A local lake | add `COMPOSE_PROFILES=lake` | Also an S3 endpoint ([Versity S3 Gateway](https://github.com/versity/versitygw)) and the Apache Iceberg project's REST catalog, for development and tests. Works with or without the workers |
| DuckDB workers | `COMPOSE_FILE=compose.yml:compose.pool.yml` | Also two [Hardcharger](https://github.com/gizmodata/hardcharger) workers, DuckDB servers that stream Arrow results. See [DuckDB workers](#duckdb-workers) for what OBSL does with them today |

Most deployments need OBSL alone. The workers are for lake sources whose load
needs more than one DuckDB process.

## Start

```bash
cd deploy/compose
cp .env.example .env            # which files, images, ports
cp obsl.env.example obsl.env    # OBSL's settings: models and data sources
mkdir -p models data            # your models, and the files they read
docker compose up -d --wait
curl http://localhost:8080/v1/models
```

As shipped, OBSL starts with the example model built into its image. Its data
is not in the image, so it compiles queries but cannot run them.

**Your own models.** Put the model files in `models/` and the files they read
in `data/`. In `obsl.env`, uncomment `MODEL_DIR=/models` and name the files in
`MODEL_FILES`, add the data sources they use
([Data Sources](https://ralforion.com/orionbelt-semantic-layer/guide/data-sources/)),
and run `docker compose up -d --wait` again.

OBSL is built from this checkout. To run a published image instead, set
`OBSL_IMAGE` in `.env`, for example
`OBSL_IMAGE=ralforion/orionbelt-semantic-layer-flight:<version>`.

| File | What it holds | Committed |
|---|---|---|
| `compose.yml` | OBSL, and the local lake behind the `lake` profile | yes |
| `compose.pool.yml` | The DuckDB workers | yes |
| `.env` | Which files make up the deployment, images, ports, the workers' admin password | no: copy `.env.example` |
| `obsl.env` | OBSL's settings, as for `docker run --env-file`: models, data sources | no: copy `obsl.env.example` |
| `secrets/hardcharger-init.sql` | What each worker runs at start, credentials included | no: copy `hardcharger-init.sql.example` |
| `models/`, `data/` | The models (at `/models`), and files they read (at `/data`) | no |

- **Ports.** OBSL publishes REST on 8080 and Arrow Flight SQL on 8815, which
  answers when `FLIGHT_ENABLED=true`. Set `OBSL_PORT` and `OBSL_FLIGHT_PORT` to
  change them. Nothing else publishes a port.
- **Federation's spill files** go to the volume `obsl-spill`, mounted at
  `/app/spill`. See [Docker](https://ralforion.com/orionbelt-semantic-layer/getting-started/docker/#spilling-federated-queries-to-disk)
  for its size.
- **The network** is an ordinary bridge network. Not `internal: true`: an
  internal network has no route off the host, so nothing could reach object
  storage or a catalog. The workers are private because they publish no ports.

## The local lake

`COMPOSE_PROFILES=lake` adds `lake-s3` (S3 at `lake-s3:9000`, keys `lake-key` /
`lake-secret`, bucket `warehouse`) and `lake-rest` (the Iceberg REST catalog at
`http://lake-rest:8181`, warehouse `s3://warehouse/`). The lake keeps its data
in its containers: `docker compose down` deletes it.

The example files read the lake only once you say so, since without the
profile there is no catalog to attach:

| File | Uncomment | Then |
|---|---|---|
| `obsl.env` | The block after "With the local lake" | A DuckDB source `lake` reads files under `/data` and the lake's Iceberg tables in process |
| `secrets/hardcharger-init.sql` | The block after "With the local lake" | The workers read the lake too. Uncomment it only with the profile: a worker whose init SQL cannot attach the catalog does not start |

For your own S3 and catalog, uncomment the same blocks and change the values.

## DuckDB workers

`compose.pool.yml` adds the workers `hardcharger-1` and `hardcharger-2`. To add
one, copy a service and give it the next name.

**Status:** the workers start and serve each OBSL source's user, but OBSL does
not send queries to them yet. That arrives with OBSL's pool backend; until then
OBSL runs every source in process, as without the workers.

- **Reached by name.** The workers publish no ports. On the project network
  they are `hardcharger://hardcharger-1?tls=false`, and so on.
- **The init SQL** (`secrets/hardcharger-init.sql`, mounted as a Compose secret)
  runs at each worker's start. As shipped, it creates one user per OBSL source
  with the locations it may read (`GRANT READ FILESYSTEM ON LOCATION '/data/'`).
  Its lake block adds DuckDB's extensions, the storage and catalog secrets, the
  `ATTACH` of the Iceberg catalog, and the grants on them (`READ OBJECT
  STORAGE`, `SELECT ON CATALOG`). A source's user cannot create secrets, attach
  or read outside its grants. Each worker keeps its own users, created by the
  same file. On Linux the file must be readable by the worker's user (uid
  999): `chmod 644`.
- **Extensions.** `INSTALL` downloads them when a worker's container is
  created, so with the lake block the workers need DuckDB's extension
  repository then.
- **Same paths.** `./data` is mounted at `/data` in OBSL and in every worker,
  because the grants and the models name that path.
- **Size.** Each worker gets `HC_CPUS` (2) and `HC_MEMORY` (4g), and DuckDB
  `HC_DUCKDB_MEMORY` (3GB), below the container's. It runs `HC_CONCURRENCY`
  (4) statements at once and queues `HC_QUEUE` (8) more; beyond that a
  statement is refused with HTTP 429 before it starts. Size the memory and the
  queue together: concurrent large results need memory of their own.
- **Health.** A worker is healthy when its `/health` reports `serving`; a
  draining worker reports `not_serving` and counts as unhealthy.

When the catalog or object storage is down (checked by
`tests/docker/test_compose.sh`):

| What is down | When | What happens |
|---|---|---|
| The catalog | At a worker's start | The worker exits, since its init SQL cannot attach the catalog, and Docker restarts it until the catalog answers; it was healthy again 12 to 15 s after the catalog. With the local lake, the workers wait for its catalog at start |
| The catalog | While a worker runs | Iceberg queries fail at once with the catalog's error. Files still read, and the worker recovers on its own |
| Object storage | At start or while running | The worker starts and reads files, but an Iceberg query waits until the statement timeout (`HC_QUERY_TIMEOUT`, 120 s) |

### A worker on another machine

Give it a certificate and key, and only HTTPS is served:

```bash
HARDCHARGER_TLS_CERT=/tls/cert.pem
HARDCHARGER_TLS_KEY=/tls/key.pem
```

The driver then reaches it as `hardcharger://node2:31337` (TLS unless
`?tls=false`), with `tls_root_certs` naming a private CA. Client certificates
(`HARDCHARGER_MTLS_CA_CERT`) can restrict it to OBSL. The worker needs the same
init SQL, and the files under the same paths.

## Test

```bash
./tests/docker/test_compose.sh            # builds OBSL from the checkout
./tests/docker/test_compose.sh --no-build # uses the image built before
```

It runs the three setups as the Compose project `obsl-compose-test` on ports
18080 and 18815, and reads no `.env` of this directory.
