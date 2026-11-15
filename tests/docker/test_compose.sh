#!/usr/bin/env bash
# Tests for the Docker Compose setup in deploy/compose: OBSL alone, with the
# local lake (COMPOSE_PROFILES=lake), and with the DuckDB workers
# (compose.pool.yml) with and without the lake, including what a worker does
# when the catalog or S3 is down. The shipped examples (obsl.env.example,
# hardcharger-init.sql.example) run as they are, and with their lake blocks
# uncommented. The models are in compose_models/.
#
# Usage: ./tests/docker/test_compose.sh [--no-build] [--keep]
#   --no-build  use the OBSL image built before (orionbelt-semantic-layer-api:compose)
#   --keep      leave the containers running at the end
#
# Needs Docker with Compose 2.24 or later, and internet access for the images,
# the driver and DuckDB's extensions. Runs as Compose project obsl-compose-test
# on host ports 18080 and 18815, and reads no .env of the directory.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
COMPOSE_DIR="$ROOT/deploy/compose"
PROBE_SCRIPT="$ROOT/tests/docker/compose_probe.py"
FIXTURE_MODELS="$ROOT/tests/docker/compose_models"
PROJECT=obsl-compose-test
NETWORK="${PROJECT}_obsl"
HOST_PORT=18080
PROBE_IMAGE=obsl-compose-probe:adbc-driver-hardcharger-2.5.1
STORE_USER=obsl_lake
STORE_PASSWORD=change-me      # the example init SQL's
BUILD=1
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --no-build) BUILD=0 ;;
        --keep) KEEP=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

PASSED=0
FAILED=0
TESTS=()
WORK=$(mktemp -d "${TMPDIR:-/tmp}/obsl-compose-test.XXXXXX")

log() { printf "\033[1m%s\033[0m\n" "$*"; }
pass() { PASSED=$((PASSED + 1)); TESTS+=("PASS: $1"); printf "  \033[32mPASS\033[0m %s\n" "$1"; }
fail() { FAILED=$((FAILED + 1)); TESTS+=("FAIL: $1: $2"); printf "  \033[31mFAIL\033[0m %s: %s\n" "$1" "$2"; }
check() {  # check NAME EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi
}
pause() { python3 -c "import time; time.sleep($1)"; }

# Compose for one setup: FILES holds the -f and --profile options.
FILES=()
dc() { (cd "$COMPOSE_DIR" && docker compose -p "$PROJECT" --env-file "$WORK/compose.env" "${FILES[@]}" "$@"); }
alone() { FILES=(-f compose.yml); }
with_lake() { FILES=(-f compose.yml --profile lake); }
with_workers() { FILES=(-f compose.yml -f compose.pool.yml); }
with_pool() { FILES=(-f compose.yml -f compose.pool.yml --profile lake); }

cleanup() {
    if [[ $KEEP -eq 0 ]]; then
        with_pool
        dc down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

state() {  # state SERVICE -> "status health restarts"
    docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.RestartCount}}' \
        "${PROJECT}-$1-1"
}
wait_healthy() {  # wait_healthy SERVICE SECONDS -> seconds taken, or "timeout"
    local start=$SECONDS
    while (( SECONDS - start < $2 )); do
        if [[ "$(state "$1")" == "running healthy "* ]]; then echo $((SECONDS - start)); return; fi
        pause 1
    done
    echo timeout
}

# POST a query to OBSL's REST API; prints the rows as JSON, or the error.
query() {
    curl -s -X POST "http://localhost:${HOST_PORT}/v1/query/execute" \
        -H 'Content-Type: application/json' -d "$1" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["rows"]) if "rows" in d else d)'
}
EXPECTED_ROWS='[["c0", 40.0], ["c1", 50.0]]'
FILES_QUERY='{"select":{"dimensions":["Customer"],"measures":["Revenue"]},"orderBy":[{"field":"Customer"}]}'
LAKE_QUERY='{"select":{"dimensions":["Lake Customer"],"measures":["Lake Revenue"]},"orderBy":[{"field":"Lake Customer"}]}'

# Run one statement on a worker as the source's store user, from the project's
# network. Prints the probe's JSON with the request ids left out.
probe() {  # probe WORKER SQL [PASSWORD]
    docker run --rm --network "$NETWORK" -v "$PROBE_SCRIPT:/probe.py:ro" "$PROBE_IMAGE" \
        python /probe.py "$1" "$STORE_USER" "${3:-$STORE_PASSWORD}" "$2"
}
probe_rows() {  # the rows, or the error
    probe "$@" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps([list(r.values()) for r in d["rows"]]) if d["ok"] else "error: " + d["error"])'
}
probe_refused() {  # prints the error code the worker answered with
    probe "$@" | python3 -c 'import json,re,sys; d=json.load(sys.stdin); m=re.search(r"\b(UNAUTHORIZED|UNAUTHENTICATED|INVALID_ARGUMENT|TIMEOUT)\b", d.get("error","")); print(m.group(1) if m else ("ok" if d["ok"] else d["error"]))'
}

# ── Fixtures ─────────────────────────────────────────────────────────

mkdir -p "$WORK/models" "$WORK/data" "$WORK/secrets"
cp "$FIXTURE_MODELS"/*.yaml "$WORK/models/"

# OBSL's settings: the example as shipped; with its lake block and a model
# uncommented; and settings of one's own, a DuckDB source over the Parquet file.
cp "$COMPOSE_DIR/obsl.env.example" "$WORK/obsl-shipped.env"
sed -E -e 's/^# (OBSL_DATABASES=|DB_LAKE_)/\1/' -e 's/^# (MODEL_DIR=.*)/\1/' \
    -e 's/^# MODEL_FILES=.*/MODEL_FILES=lake.yaml/' "$COMPOSE_DIR/obsl.env.example" > "$WORK/obsl-lake.env"
cat > "$WORK/obsl-files.env" <<'EOF'
MODEL_DIR=/models
MODEL_FILES=files.yaml
QUERY_EXECUTE=true
OBSL_DATABASES=lake
DB_LAKE_TYPE=duckdb
DB_LAKE_DUCKDB_DATABASE=:memory:
DB_LAKE_DUCKDB_ALLOWED_PATHS=/data/
EOF
# The workers' init SQL: as shipped, and with its lake block uncommented.
cp "$COMPOSE_DIR/hardcharger-init.sql.example" "$WORK/secrets/init-shipped.sql"
sed -E 's/^-- (INSTALL|LOAD|CREATE OR REPLACE SECRET|ATTACH|GRANT) /\1 /' \
    "$COMPOSE_DIR/hardcharger-init.sql.example" > "$WORK/secrets/init-lake.sql"
chmod 644 "$WORK"/secrets/*.sql
for f in obsl-lake.env secrets/init-lake.sql; do
    if grep -q -E '^(# (OBSL_DATABASES|DB_LAKE_|MODEL_)|-- (INSTALL|LOAD|CREATE|ATTACH|GRANT) )' "$WORK/$f"; then
        echo "a lake line of $f stayed commented: the example's layout changed" >&2
        exit 1
    fi
done

compose_env() {  # compose_env OBSL_ENV_FILE [INIT_SQL]: with INIT_SQL, the workers' settings too
    {
        echo "OBSL_MODELS=$WORK/models"
        echo "OBSL_DATA=$WORK/data"
        echo "OBSL_ENV_FILE=$1"
        echo "OBSL_PORT=$HOST_PORT"
        echo "OBSL_FLIGHT_PORT=18815"
        if [[ -n "${2:-}" ]]; then
            echo "HARDCHARGER_ADMIN_PASSWORD=compose-test-admin"
            echo "HARDCHARGER_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
            echo "HARDCHARGER_INIT_SQL=$2"
            # A short statement timeout, so a query on a stopped S3 ends within the test.
            echo "HC_QUERY_TIMEOUT=10"
        fi
    } > "$WORK/compose.env"
}

# ── Build ────────────────────────────────────────────────────────────

compose_env "$WORK/obsl-shipped.env"
alone
if [[ $BUILD -eq 1 ]]; then
    log "Building the OBSL image from this checkout..."
    dc build >/dev/null
fi
log "Building the probe image (Python and adbc-driver-hardcharger)..."
printf 'FROM python:3.12-slim\nRUN pip install --no-cache-dir adbc-driver-hardcharger==2.5.1 pyarrow\n' |
    docker build -q -t "$PROBE_IMAGE" - >/dev/null
docker run --rm --user 0 -v "$WORK/data:/out" --entrypoint python orionbelt-semantic-layer-api:compose -c \
    "import duckdb; duckdb.sql(\"COPY (SELECT i AS id, 'c' || (i % 2) AS cust, (i * 2.0)::DOUBLE AS amount FROM range(10) t(i)) TO '/out/orders.parquet' (FORMAT parquet)\")"
dc down -v --remove-orphans >/dev/null 2>&1 || true

# ── 1. The files ─────────────────────────────────────────────────────

log "1. The Compose files"
alone
if dc config -q 2>/dev/null; then pass "compose.yml alone needs no pool variables"; else fail "compose.yml alone" "config failed"; fi
FILES=(-f compose.yml -f compose.pool.yml)
out=$(dc config -q 2>&1 || true)
if [[ "$out" == *HARDCHARGER_ADMIN_PASSWORD* ]]; then pass "compose.pool.yml refuses to start without HARDCHARGER_ADMIN_PASSWORD"; else fail "pool without password" "$out"; fi

# ── 2. OBSL alone ────────────────────────────────────────────────────

log "2. OBSL alone"
alone
compose_env "$WORK/obsl-shipped.env"
dc up -d --wait >/dev/null 2>&1 || true
check "only OBSL runs" "obsl" "$(dc ps --format '{{.Service}}' | sort | tr '\n' ' ' | xargs)"
check "OBSL is healthy with obsl.env.example as shipped" "running healthy" "$(state obsl | cut -d' ' -f1-2)"
check "with obsl.env.example as shipped, OBSL serves the image's example model" "orionbelt_1_commerce" \
    "$(curl -s "http://localhost:${HOST_PORT}/v1/models" | python3 -c 'import json,sys; print(" ".join(m["name"] for m in json.load(sys.stdin)["models"]))')"
compose_env "$WORK/obsl-files.env"
dc up -d --wait >/dev/null 2>&1 || true
check "with a model and a source of its own, OBSL reads a Parquet file in process" "$EXPECTED_ROWS" "$(query "$FILES_QUERY")"

# ── 3. OBSL with the local lake ──────────────────────────────────────

log "3. OBSL with the local lake (COMPOSE_PROFILES=lake)"
seed_lake() {  # create lake.sales.orders through OBSL's DuckDB, as another writer would
    dc exec -T obsl python - <<'EOF'
import time
import duckdb

setup = (
    "CREATE OR REPLACE SECRET lake_s3 (TYPE s3, KEY_ID 'lake-key', SECRET 'lake-secret', "
    "REGION 'us-east-1', ENDPOINT 'lake-s3:9000', URL_STYLE 'path', USE_SSL false); "
    "ATTACH IF NOT EXISTS 'warehouse' AS lake (TYPE iceberg, ENDPOINT 'http://lake-rest:8181', "
    "AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none')"
)
con = duckdb.connect()
for attempt in range(60):
    try:
        con.execute(setup)
        break
    except duckdb.Error:
        if attempt == 59:
            raise
        time.sleep(1)
con.execute("CREATE SCHEMA IF NOT EXISTS lake.sales")
con.execute(
    "CREATE TABLE lake.sales.orders AS SELECT i AS id, 'c' || (i % 2) AS cust, "
    "(i * 2.0)::DOUBLE AS amount FROM range(10) t(i)"
)
EOF
}
with_lake
compose_env "$WORK/obsl-lake.env"
dc up -d --wait >/dev/null 2>&1 || true
check "the lake is healthy" "running healthy running healthy" \
    "$(echo "$(state lake-s3 | cut -d' ' -f1-2) $(state lake-rest | cut -d' ' -f1-2)")"
seed_lake
check "with obsl.env.example's lake block, OBSL reads an Iceberg table in process" "$EXPECTED_ROWS" "$(query "$LAKE_QUERY")"

PARQUET="SELECT cust, sum(amount) FROM read_parquet('/data/orders.parquet') GROUP BY ALL ORDER BY 1"
ICEBERG="SELECT cust, sum(amount) FROM lake.sales.orders GROUP BY ALL ORDER BY 1"

# ── 4. With the workers, without the lake ────────────────────────────

log "4. OBSL and the workers, without the lake, with the shipped examples"
dc down -v --remove-orphans >/dev/null 2>&1 || true
with_workers
compose_env "$WORK/obsl-shipped.env" "$WORK/secrets/init-shipped.sql"
if dc up -d --wait >/dev/null 2>&1; then pass "up --wait starts OBSL and the workers"; else fail "up --wait" "$(dc ps -a --format '{{.Service}} {{.State}} {{.Health}}' | tr '\n' ';')"; fi
check "no lake runs" "hardcharger-1 hardcharger-2 obsl" "$(dc ps --format '{{.Service}}' | sort | tr '\n' ' ' | xargs)"
for w in hardcharger-1 hardcharger-2; do
    check "$w is healthy, with no catalog to attach" "running healthy 0" "$(state $w)"
    check "$w: reads a file in its location grant" "$EXPECTED_ROWS" "$(probe_rows $w "$PARQUET")"
    check "$w: refuses object storage without a grant" "UNAUTHORIZED" \
        "$(probe_refused $w "SELECT * FROM read_parquet('s3://warehouse/x.parquet')")"
done

# ── 5. With the workers and the lake ─────────────────────────────────

log "5. OBSL, the lake and the workers, from a cold start, with the lake blocks"
dc down -v --remove-orphans >/dev/null 2>&1 || true
with_pool
compose_env "$WORK/obsl-lake.env" "$WORK/secrets/init-lake.sql"
if dc up -d --wait >/dev/null 2>&1; then pass "up --wait starts everything at once"; else fail "up --wait" "$(dc ps -a --format '{{.Service}} {{.State}} {{.Health}}' | tr '\n' ';')"; fi
for w in hardcharger-1 hardcharger-2; do
    check "$w is healthy, without a restart while the catalog started" "running healthy 0" "$(state $w)"
done
seed_lake
published=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Label "com.docker.compose.service"}} {{.Ports}}' |
    grep -- '->' | cut -d' ' -f1 | sort -u | xargs)
check "only OBSL publishes ports" "obsl" "$published"
check "the network is not internal (the workers reach S3 and the catalog)" "false" \
    "$(docker network inspect "$NETWORK" --format '{{.Internal}}')"
reach=$(dc exec -T obsl python -c "
import socket
for w in ('hardcharger-1', 'hardcharger-2'):
    socket.create_connection((w, 31337), timeout=3).close()
print('ok')" 2>&1 | tail -1)
check "the OBSL container reaches every worker by name" "ok" "$reach"
check "OBSL still reads the lake in process" "$EXPECTED_ROWS" "$(query "$LAKE_QUERY")"

for w in hardcharger-1 hardcharger-2; do
    log "   $w, as the source's store user $STORE_USER (each worker has its own store)"
    check "$w: reads a file in its location grant" "$EXPECTED_ROWS" "$(probe_rows $w "$PARQUET")"
    check "$w: reads the Iceberg table through the catalog" "$EXPECTED_ROWS" "$(probe_rows $w "$ICEBERG")"
    check "$w: reads object storage in its grant" "[[10]]" \
        "$(probe_rows $w "SELECT count(*) FROM read_parquet('s3://warehouse/**/*.parquet')")"
    check "$w: refuses a server file outside the grant" "UNAUTHORIZED" \
        "$(probe_refused $w "SELECT * FROM read_text('/etc/hostname')")"
    check "$w: refuses object storage outside the grant" "UNAUTHORIZED" \
        "$(probe_refused $w "SELECT * FROM read_parquet('s3://other/x.parquet')")"
    check "$w: refuses CREATE SECRET to the store user" "UNAUTHORIZED" \
        "$(probe_refused $w "CREATE SECRET x (TYPE s3, KEY_ID 'a', SECRET 'b')")"
    check "$w: refuses a wrong password" "UNAUTHENTICATED" "$(probe_refused $w "SELECT 1" wrong-password)"
done

# ── 6. Failures ──────────────────────────────────────────────────────

log "6. The catalog or S3 down"
docker stop "${PROJECT}-lake-rest-1" >/dev/null
check "catalog down while a worker runs: an Iceberg query fails at once" "INVALID_ARGUMENT" \
    "$(probe_refused hardcharger-2 "$ICEBERG")"
check "catalog down while a worker runs: files still read" "$EXPECTED_ROWS" "$(probe_rows hardcharger-2 "$PARQUET")"
docker restart "${PROJECT}-hardcharger-1-1" >/dev/null
pause 15
after=$(state hardcharger-1)
if [[ "$after" != *healthy* || "$after" == *unhealthy* ]] && (( ${after##* } > 0 )); then
    pass "catalog down at a worker's start: the worker exits and Docker restarts it ($after)"
else
    fail "catalog down at start" "expected a restarting worker, got '$after'"
fi
check "catalog down while a worker runs: the worker stays healthy" "running healthy 0" "$(state hardcharger-2)"
docker start "${PROJECT}-lake-rest-1" >/dev/null
took=$(wait_healthy hardcharger-1 120)
if [[ "$took" != timeout ]]; then pass "the catalog back: the restarted worker is healthy after ${took}s"; else fail "worker recovery" "not healthy within 120s"; fi
check "the catalog back: Iceberg reads again on the running worker" "$EXPECTED_ROWS" "$(probe_rows hardcharger-2 "$ICEBERG")"

docker stop "${PROJECT}-lake-s3-1" >/dev/null
docker restart "${PROJECT}-hardcharger-2-1" >/dev/null
took=$(wait_healthy hardcharger-2 60)
if [[ "$took" != timeout ]]; then pass "S3 down at a worker's start: the worker starts (${took}s)"; else fail "S3 down at start" "not healthy within 60s"; fi
check "S3 down: files still read" "$EXPECTED_ROWS" "$(probe_rows hardcharger-2 "$PARQUET")"
check "S3 down: an Iceberg query runs into the statement timeout" "TIMEOUT" "$(probe_refused hardcharger-2 "$ICEBERG")"
docker start "${PROJECT}-lake-s3-1" >/dev/null
pause 5
check "S3 back: Iceberg reads again" "$EXPECTED_ROWS" "$(probe_rows hardcharger-2 "$ICEBERG")"

# ── Summary ──────────────────────────────────────────────────────────

echo ""
log "Results: ${PASSED} passed, ${FAILED} failed ($((PASSED + FAILED)) total)"
if [[ $FAILED -gt 0 ]]; then
    for t in "${TESTS[@]}"; do
        if [[ "$t" == FAIL* ]]; then printf "  \033[31m%s\033[0m\n" "$t"; fi
    done
    exit 1
fi
