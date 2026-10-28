---
description: "Run OrionBelt from the published multi-platform Docker images: the REST API, the Arrow Flight SQL build and the Gradio UI, with the ports and environment each expects."
---

# Docker

## Docker Hub

Pre-built multi-platform images (linux/amd64, linux/arm64) are available on [Docker Hub](https://hub.docker.com/repositories/ralforion):

```bash
# API-only (REST API on :8080)
docker pull ralforion/orionbelt-semantic-layer-api
docker run -p 8080:8080 ralforion/orionbelt-semantic-layer-api

# API + Arrow Flight SQL (REST on :8080, Flight on :8815)
docker pull ralforion/orionbelt-semantic-layer-flight
docker run -p 8080:8080 -p 8815:8815 --env-file .env ralforion/orionbelt-semantic-layer-flight

# UI (Gradio on :7860, connects to API)
docker pull ralforion/orionbelt-semantic-layer-ui
docker run -p 7860:7860 \
  -e API_BASE_URL=http://host.docker.internal:8080 \
  ralforion/orionbelt-semantic-layer-ui
```

See [Drivers & Flight SQL](../drivers.md) for Flight SQL configuration and BI tool setup (DBeaver, Tableau, Power BI).

The `.env` passed with `--env-file` carries the database credentials. The API
image carries the DuckDB driver only; the Flight image carries all eight
(`docker build --build-arg OB_EXTRA=flight .` builds an API image with them). To serve
several databases from one container, two of one dialect included, declare
them as [named data sources](../guide/data-sources.md) (`OBSL_DATABASES`); the
same page shows how to pass them, and their secrets, on
[Cloud Run](../guide/data-sources.md#deployment).

## Spilling federated queries to disk

A [query across data sources](../guide/data-sources.md#running-a-federated-query)
can spill its large legs to disk instead of holding them in memory. It is off
until `FEDERATION_SPILL_DIR` is set. In a container, give it a volume:

```bash
docker volume create obsl-spill
docker run -p 8080:8080 --env-file .env \
  -v obsl-spill:/app/spill \
  -e FEDERATION_SPILL_DIR=/app/spill \
  ralforion/orionbelt-semantic-layer-flight
```

```yaml
# docker compose
services:
  api:
    image: ralforion/orionbelt-semantic-layer-flight
    env_file: .env
    environment:
      FEDERATION_SPILL_DIR: /app/spill
    volumes:
      - obsl-spill:/app/spill
volumes:
  obsl-spill:
```

| Question | Answer |
|---|---|
| Which path? | `/app/spill`. The API and Flight images create it owned by their non-root `app` user, and Docker gives a new, empty named volume the owner of the directory it is first mounted on, so the server can write to it. A volume created earlier keeps the owner it has. A volume mounted on a path the image does not have starts out owned by root, and every federated query is then refused, naming `FEDERATION_SPILL_DIR` |
| A host directory instead? | It keeps the host's owner. Make it writable for the image's `app` user, whose id `docker run --rm --entrypoint id <image> app` prints, e.g. `sudo chown <uid> /srv/obsl-spill`, then mount it with `-v /srv/obsl-spill:/app/spill` |
| Without a volume? | The files then go to the container's writable layer: on the host's disk, but through the overlay filesystem (slower), and counted against the container rather than a volume you can size and watch |
| `--tmpfs`? | No. A tmpfs is memory, so a spilled leg takes the memory it was meant to free. The same holds for Cloud Run, whose filesystem is in memory: leave `FEDERATION_SPILL_DIR` unset there |
| How large? | Each query's legs spill at most `FEDERATION_SPILL_MAX_MB`, all queries' legs together at most `FEDERATION_SPILL_BUDGET_MB`, and each query's DuckDB combine step at most `FEDERATION_SPILL_MAX_MB` more. The most the directory holds is `FEDERATION_SPILL_BUDGET_MB + FEDERATION_MAX_CONCURRENT × FEDERATION_SPILL_MAX_MB`: 16384 + 4 × 4096 MB = 32 GB with the defaults. Docker does not cap a named volume's size; it takes from the disk Docker stores volumes on, so that disk needs this much free, or lower the limits to fit it (a 10 GB disk: `FEDERATION_SPILL_BUDGET_MB=4096`, `FEDERATION_SPILL_MAX_MB=1024`, which with 4 queries at once takes 8 GB at most) |
| Docker Desktop? | Volumes live on the Desktop VM's virtual disk, shared with every image, container and build cache (Settings > Resources > Virtual disk limit). `docker system df` shows what takes it, and `df -h /app/spill` inside the container what is left |
| When the disk fills? | A query that cannot create its directory, or runs out of space while writing a leg, is refused with HTTP 503 naming `FEDERATION_SPILL_DIR` and the operating system's reason (`FEDERATION_SPILL_FULL`), not reported as a database failure. Set the limits below what the disk holds, so the budget refuses a query before the disk does |
| Kubernetes? | An `emptyDir` volume without `medium: Memory`, with `sizeLimit` at least the size above: the kubelet evicts a pod that writes past it |
| Cleaning up? | Each query removes its own files when it ends, failed or not, so the volume holds only what the queries running now spilled. Restarting the container mid-query can leave a query's `obsl-federation-*` directory behind; any of them is safe to delete while the server is stopped |

## Build from Source

Two separate images — API-only (fast) and UI (with Gradio):

```bash
# API image (no Gradio, fast cold starts)
docker build -t orionbelt-semantic-layer-api .
docker run -p 8080:8080 orionbelt-semantic-layer-api

# UI image (Gradio, connects to API)
docker build -f Dockerfile.ui -t orionbelt-semantic-layer-ui .
docker run -p 7860:7860 \
  -e API_BASE_URL=http://host.docker.internal:8080 \
  orionbelt-semantic-layer-ui
```

The API is available at `http://localhost:8080`. The UI is at `http://localhost:7860`. Sessions are ephemeral (in-memory, lost on container restart).

## Integration Tests

```bash
# Build image and run 15 endpoint tests
./tests/docker/test_docker.sh

# Skip build (use existing image)
./tests/docker/test_docker.sh --no-build

# Run 30 tests against a live Cloud Run deployment
./tests/cloudrun/test_cloudrun.sh https://orionbelt-semantic-layer-mw2bqg2mva-ew.a.run.app
```

## Cloud Run Deployment

OrionBelt deploys as **two separate Cloud Run services** behind a shared load balancer:

```
Load Balancer (single IP)
  ├── /ui/*     → orionbelt-ui   (Gradio)
  └── /*        → orionbelt-api  (FastAPI)
```

The API image (`Dockerfile`) excludes Gradio for faster cold starts (~2-3s vs ~12s), while the UI image (`Dockerfile.ui`) connects to the API via `API_BASE_URL`. Cloud Armor provides WAF protection.
