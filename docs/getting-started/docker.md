# Docker

## Docker Hub

Pre-built multi-platform images (linux/amd64, linux/arm64) are available on [Docker Hub](https://hub.docker.com/repositories/ralforion):

```bash
# API-only (REST API on :8080)
docker pull ralforion/orionbelt-api
docker run -p 8080:8080 ralforion/orionbelt-api

# API + Arrow Flight SQL (REST on :8080, Flight on :8815)
docker pull ralforion/orionbelt-flight
docker run -p 8080:8080 -p 8815:8815 --env-file .env ralforion/orionbelt-flight

# UI (Gradio on :7860, connects to API)
docker pull ralforion/orionbelt-ui
docker run -p 7860:7860 \
  -e API_BASE_URL=http://host.docker.internal:8080 \
  ralforion/orionbelt-ui
```

See [Drivers & Flight SQL](../drivers.md) for Flight SQL configuration and BI tool setup (DBeaver, Tableau, Power BI).

## Build from Source

Two separate images — API-only (fast) and UI (with Gradio):

```bash
# API image (no Gradio, fast cold starts)
docker build -t orionbelt-api .
docker run -p 8080:8080 orionbelt-api

# UI image (Gradio, connects to API)
docker build -f Dockerfile.ui -t orionbelt-ui .
docker run -p 7860:7860 \
  -e API_BASE_URL=http://host.docker.internal:8080 \
  orionbelt-ui
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
