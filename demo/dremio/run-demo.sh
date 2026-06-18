#!/usr/bin/env bash
# One-command bring-up for the OrionBelt <-> Dremio semantic-sidecar demo.
#
#   demo/dremio/run-demo.sh             # build assets, up, wait, bootstrap
#   NO_BUILD=1 demo/dremio/run-demo.sh  # skip docker image build
#   DOWN=1     demo/dremio/run-demo.sh  # tear the stack down (and volumes)
#
# Leaves the stack UP so you can drive the Dremio UI (http://localhost:19047)
# and the OrionBelt playground (http://localhost:17860) during the call.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

cd "$REPO_ROOT"

if [[ -n "${DOWN:-}" ]]; then
    echo "Tearing down demo stack..."
    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans
    exit 0
fi

echo "[1/4] Building demo assets (Parquet + Dremio-dialect model)..."
uv run python "$SCRIPT_DIR/build_assets.py"

echo "[2/4] Starting compose stack..."
if [[ -z "${NO_BUILD:-}" ]]; then
    docker compose -f "$COMPOSE_FILE" up -d --build
else
    docker compose -f "$COMPOSE_FILE" up -d
fi

echo "[3/4] Waiting for Dremio REST API (cold start ~30-60s)..."
for _ in $(seq 1 90); do
    if curl -fsS http://localhost:19047/apiv2/server_status >/dev/null 2>&1; then
        echo "  Dremio is up."
        break
    fi
    sleep 2
done

echo "[4/4] Bootstrapping Dremio (S3 source, dataset promotion, pgwire source)..."
DREMIO_REST_URL="http://localhost:19047" uv run python "$SCRIPT_DIR/bootstrap.py"

cat <<'EOF'

------------------------------------------------------------------
Demo is ready.

  Dremio UI    : http://localhost:19047   (obsl_admin / obsl_admin_pw_123!)
  OrionBelt UI : http://localhost:17860
  MinIO console: http://localhost:19001   (minioadmin / minioadmin)

Try in Dremio's SQL Runner:

  -- RAW lakehouse SQL over the Parquet:
  SELECT co.countryname, SUM(s.salesamount)
  FROM lake.commerce.sales s
  JOIN lake.commerce.clients c ON s.salesclient = c.clientid
  JOIN lake.commerce.countries co ON c.clientcountryid = co.countryid
  GROUP BY co.countryname ORDER BY 2 DESC LIMIT 5;

  -- GOVERNED semantic query (federated into OrionBelt, pushed back via Flight):
  SELECT "Country Name", "Total Sales"
  FROM orionbelt.commerce.model
  ORDER BY "Total Sales" DESC LIMIT 5;

Tear down: DOWN=1 demo/dremio/run-demo.sh
------------------------------------------------------------------
EOF
