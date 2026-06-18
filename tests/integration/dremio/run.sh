#!/usr/bin/env bash
# End-to-end runner for the Dremio ↔ OBSL pgwire compat suite.
#
# Brings up both containers, waits for the Dremio REST API to respond,
# runs the dremio-marked pytest suite, and tears everything down.
#
# Usage:
#   tests/integration/dremio/run.sh              # build, run, teardown
#   KEEP_STACK=1 tests/integration/dremio/run.sh # leave containers up afterwards
#   NO_BUILD=1 tests/integration/dremio/run.sh   # skip docker compose build

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." &>/dev/null && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

cd "$REPO_ROOT"

cleanup() {
    if [[ -z "${KEEP_STACK:-}" ]]; then
        echo "Tearing down compose stack..."
        docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if [[ -z "${NO_BUILD:-}" ]]; then
    echo "Building OBSL image..."
    docker compose -f "$COMPOSE_FILE" build
fi

echo "Starting compose stack..."
docker compose -f "$COMPOSE_FILE" up -d

echo "Waiting for Dremio REST API (max ~3 min)..."
for _ in {1..90}; do
    if curl -fsS http://localhost:19047/apiv2/server_status >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "Running pytest -m dremio..."
DREMIO_REST_URL="http://localhost:19047" \
OBSL_PGWIRE_HOST="obsl" \
OBSL_MODEL_NAME="orionbelt_1_commerce" \
    uv run pytest -m dremio tests/integration/dremio/ "$@"
